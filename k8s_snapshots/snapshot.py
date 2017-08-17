import asyncio
import inspect
from datetime import timedelta
from typing import Dict, Tuple, List, Iterable, Callable, Union, \
    Awaitable, Container

import aiohttp
import pendulum
import re
import structlog
from tarsnapper.expire import expire

from k8s_snapshots import events, errors, serialize
from k8s_snapshots.asyncutils import run_in_executor
from k8s_snapshots.context import Context
from k8s_snapshots.rule import Rule
from .backends.abstract import Snapshot, NewSnapshotIdentifier, SnapshotStatus


_logger = structlog.get_logger(__name__)


async def expire_snapshots(ctx, rule: Rule):
    """
    Expire existing snapshots for the rule.
    """
    _log = _logger.new(
        rule=rule,
    )

    _log.debug(events.Expiration.STARTED)

    snapshots_objects = filter_snapshots_by_rule(await load_snapshots(ctx), rule)
    snapshots_with_date = {s: s.created_at for s in snapshots_objects}

    to_keep = expire(snapshots_with_date, rule.deltas)
    expired_snapshots: List[str] = []
    kept_snapshots = []

    for snapshot, snapshot_time_created in snapshots_with_date.items():
        _log_inner = _log.new(
            snapshot_name=snapshot.name,
            snapshot_time_created=snapshot_time_created,
            key_hints=[
                'snapshot_name',
                'snapshot_time_created',
            ]
        )
        
        if snapshot in to_keep:
            _log_inner.debug(events.Expiration.KEPT)
            kept_snapshots.append(snapshot.name)
            continue

        if snapshot not in to_keep:
            _log_inner.debug(events.Expiration.DELETE)

            # TODO: Deleting a snapshot is usually an async process too,
            # and to be completely accurate, we should wait for it to complete.
            await run_in_executor(
                lambda: ctx.backend.delete_snapshot(ctx, snapshot)
            )
            expired_snapshots.append(snapshot.name)

    _log.info(
        events.Expiration.COMPLETE,
        snapshots={
            'expired': expired_snapshots,
            'kept': kept_snapshots,
        }
    )


async def make_backup(ctx, rule):
    """Execute a single backup job.

    1. Create the snapshot
    2. Wait until the snapshot is finished.
    3. Expire old snapshots
    """
    snapshot_name = new_snapshot_name(ctx, rule)

    _log = _logger.new(
        snapshot_name=snapshot_name,
        rule=rule
    )

    try:
        snapshot_identifier = await create_snapshot(
            ctx,
            rule.gce_disk,
            rule.gce_disk_zone,
            snapshot_name,
            snapshot_description=serialize.dumps(rule),
        )

        _log.debug(
            'snapshot.operation-started',
            key_hints=[
                'snapshot_name'
            ],
            snapshot_identifier=snapshot_identifier
        )

        await poll_for_status(
            lambda: get_snapshot_status(ctx, snapshot_identifier),
            retry_for=(SnapshotStatus.PENDING,)
        )

    # TODO: If there is some kind of coding error, we should crash I think.
    except Exception as exc:
        _log.exception(
            events.Snapshot.ERROR,
            key_hints=['snapshot_name', 'rule.name']
        )
        raise errors.SnapshotCreateError(
            'Error creating snapshot'
        ) from exc

    await set_snapshot_labels(
        ctx,
        snapshot_identifier,
        snapshot_labels(ctx),
    )

    _log.info(
        events.Snapshot.CREATED,
        snapshot_identifier=snapshot_identifier,
        key_hints=['snapshot_name', 'rule.name'],
    )

    ping_url = ctx.config.get('ping_url')
    if ping_url:
        with aiohttp.ClientSession() as session:
            response = await session.request('GET', ping_url)
            _log.info(
                events.Ping.SENT,
                status=response.status,
                url=ping_url,
            )

    await expire_snapshots(ctx, rule)


async def create_snapshot(
        ctx: Context,
        disk_name: str,
        disk_zone: str,
        snapshot_name: str,
        snapshot_description: str
) -> NewSnapshotIdentifier:
    _log = _logger.new(
        disk_name=disk_name,
        disk_zone=disk_zone,
        snapshot_name=snapshot_name,
        snapshot_description=snapshot_description
    )

    _log.info(
        events.Snapshot.START,
        key_hints=['snapshot_name', 'rule.name']
    )
    return await run_in_executor(
        lambda: ctx.backend.create_snapshot(
            ctx,
            disk_name,
            disk_zone,
            snapshot_name,
            snapshot_description
        )
    )


async def poll_for_status(
    refresh_func: Callable[..., Union[Dict, Awaitable[Dict]]],
    retry_for: Container[str],
    sleep_time: int=1,
):
    """
    Call refresh_func until the return value  is not one of the values
    in ``retry_for``.

    Parameters
    ----------
    refresh_func
        Callable that returns either

        -   The new version of the resource.
        -   An awaitable for the new version of the resource.
    retry_for
        A list of statuses to retry for.
    status_key
    sleep_time
        The time, in seconds, to sleep for between calls.

    Returns
    -------

    """
    _log = _logger.new()
    refresh_count = 0
    time_start = pendulum.now()

    while True:
        await asyncio.sleep(sleep_time)  # Sleep first
        
        result = refresh_func()
        if inspect.isawaitable(result):
            result = await result

        _log.debug(
            'poll-for-status.refreshed',
            key_hints=[
                'result'
            ],
            refresh_count=refresh_count,
            result=result
        )

        if not result in retry_for:
            break

        refresh_count += 1

    time_taken = pendulum.now() - time_start

    _log.debug(
        'poll-for-status.done',
        key_hints=[
            'refresh_count',
            'time_taken',
        ],
        refresh_count=refresh_count,
        time_start=time_start,
        time_taken=time_taken
    )

    return result


def snapshot_author_label(ctx: Context) -> Tuple[str, str]:
    return (
        ctx.config['snapshot_author_label_key'],
        ctx.config['snapshot_author_label']
    )


def snapshot_labels(ctx: Context) -> Dict:
    return dict([snapshot_author_label(ctx)])


async def set_snapshot_labels(
    ctx: Context,
    snapshot_identifier: NewSnapshotIdentifier,
    labels: Dict
):
    _log = _logger.new(
        snapshot_identifier=snapshot_identifier,
        labels=labels,
    )
        
    _log.debug(
        'snapshot.set-labels',
        key_hints=['body.labels']
    )
    return await run_in_executor(
        lambda: ctx.backend.set_snapshot_labels(ctx, snapshot_identifier, labels)
    )


def new_snapshot_name(ctx: Context, rule: Rule) -> str:
    """
    Get a new snapshot name for rule.
    Returns rule name and pendulum.now('utc') formatted according to settings.
    """

    time_str = re.sub(
        r'[^-a-z0-9]', '-',
        pendulum.now('utc').format(ctx.config['snapshot_datetime_format']),
        flags=re.IGNORECASE)

    # Won't be truncated
    suffix = f'-{time_str}'

    # Will be truncated
    name_truncated = rule.name[:63 - len(suffix)]

    return f'{name_truncated}{suffix}'


async def get_snapshot_status(
        ctx: Context,
        snapshot_identifier: NewSnapshotIdentifier
):
    return await run_in_executor(
        lambda: ctx.backend.get_snapshot_status(ctx, snapshot_identifier)
    )


async def get_snapshots(ctx, reload_trigger):
    """Query the existing snapshots from the cloud provider.

    If the channel "reload_trigger" contains any value, we
    refresh the list of snapshots. This will then cause the
    next backup to be scheduled.
    """
    yield await load_snapshots(ctx)
    async for _ in reload_trigger:
        yield await load_snapshots(ctx)


async def load_snapshots(ctx) -> List[Snapshot]:
    snapshot_label_filters = dict([snapshot_author_label(ctx)])
    return await run_in_executor(
        lambda: ctx.backend.load_snapshots(ctx, snapshot_label_filters)
    )


def determine_next_snapshot(snapshots, rules):
    """
    Given a list of snapshots, and a list of rules, determine the next snapshot
    to be made.

    Returns a 2-tuple (rule, target_datetime)
    """
    next_rule = None
    next_timestamp = None

    for rule in rules:
        _log = _logger.new(rule=rule)
        # Find all the snapshots that match this rule
        snapshots_for_rule = filter_snapshots_by_rule(snapshots, rule)
        # Rewrite the list to snapshot
        snapshot_times = map(lambda s: s.created_at, snapshots_for_rule)
        # Sort by timestamp
        snapshot_times = sorted(snapshot_times, reverse=True)
        snapshot_times = list(snapshot_times)

        # There are no snapshots for this rule; create the first one.
        if not snapshot_times:
            next_rule = rule
            next_timestamp = pendulum.now('utc') + timedelta(seconds=10)
            _log.info(
                events.Snapshot.SCHEDULED,
                target=next_timestamp,
                key_hints=['rule.name', 'target'],
            )
            break

        target = snapshot_times[0] + rule.deltas[0]
        if not next_timestamp or target < next_timestamp:
            next_rule = rule
            next_timestamp = target

    if next_rule is not None and next_timestamp is not None:
        _logger.info(
            events.Snapshot.SCHEDULED,
            key_hints=['rule.name', 'target'],
            target=next_timestamp,
            rule=next_rule,
        )

    return next_rule, next_timestamp


def filter_snapshots_by_rule(snapshots: List[Snapshot], rule) -> Iterable[Snapshot]:
    def match_disk(snapshot: Snapshot):
        return snapshot.disk.disk_name == rule.gce_disk and \
            snapshot.disk.zone_name == rule.gce_disk_zone
    return filter(match_disk, snapshots)
