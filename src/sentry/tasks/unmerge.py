from __future__ import absolute_import

import logging

from django.db.models import F

from sentry.app import tsdb
from sentry.constants import DEFAULT_LOGGER_NAME, LOG_LEVELS_MAP
from sentry.event_manager import ScoreClause, generate_culprit, get_hashes_for_event, md5_from_hash
from sentry.models import Event, EventMapping, EventTag, Group, GroupHash, GroupRelease, GroupTagKey, GroupTagValue, Release, UserReport


def merge_mappings(values):
    result = {}
    for value in values:
        result.update(value)
    return result


initial_fields = {
    'culprit': lambda event: generate_culprit(
        event.data,
        event.platform,
    ),
    'data': lambda event: {
        'last_received': event.data.get('received') or float(event.datetime.strftime('%s')),
        'type': event.data['type'],
        'metadata': event.data['metadata'],
    },
    'last_seen': lambda event: event.datetime,
    'level': lambda event: LOG_LEVELS_MAP.get(
        event.get_tag('level'),
        logging.ERROR,
    ),
    'message': lambda event: event.message,
    'times_seen': lambda event: 0,
}


backfill_fields = {
    'platform': lambda data, event: event.platform,
    'logger': lambda data, event: event.get_tag('logger') or DEFAULT_LOGGER_NAME,
    'first_seen': lambda data, event: event.datetime,
    'active_at': lambda data, event: event.datetime,
    'first_release': lambda data, event: Release.objects.get(
        organization_id=event.project.organization_id,
        version=event.get_tag('sentry:release')
    ) if event.get_tag('sentry:release') else data.get('first_release', None),  # XXX: This is wildly inefficient, also double check logic.
    'times_seen': lambda data, event: data['times_seen'] + 1,
    'score': lambda data, event: ScoreClause.calculate(
        data['times_seen'] + 1,
        data['last_seen'],
    ),
}


def get_group_creation_attributes(events):
    latest_event = events[0]
    return reduce(
        lambda data, event: merge_mappings([
            data,
            {name: f(data, event) for name, f in backfill_fields.items()},
        ]),
        events,
        {name: f(latest_event) for name, f in initial_fields.items()},
    )


def get_group_backfill_attributes(group, events):
    return {
        k: v for k, v in
        reduce(
            lambda data, event: merge_mappings([
                data,
                {name: f(data, event) for name, f in backfill_fields.items()},
            ]),
            events,
            {name: getattr(group, name) for name in set(initial_fields.keys()) | set(backfill_fields.keys())},
        ).items()
        if k in backfill_fields
    }


def get_fingerprint(event):
    # TODO: This *might* need to be protected from an IndexError?
    primary_hash = get_hashes_for_event(event)[0]
    return md5_from_hash(primary_hash)


def migrate_events(source_id, destination_id, fingerprints, events):
    # XXX: This is only actually able to create a destination group and migrate
    # the group hashes if there are events that can be migrated. How do we
    # handle this if there aren't any events? We can't create a group (there
    # isn't any data to derive the aggregates from), so we'd have to mark the
    # hash as in limbo somehow...?)
    if not events:
        return destination_id

    # TODO: What happens if this ``Group`` record has been deleted?
    source = Group.objects.get(id=source_id)
    project = source.project

    if destination_id is None:
        # XXX: There is a race condition here between the (wall clock) time
        # that the migration is started by the user and when we actually
        # get to this block where the new destination is created and we've
        # moved the ``GroupHash`` so that events start being associated
        # with it. During this gap, there could have been additional events
        # ingested, and if we want to handle this, we'd need to record the
        # highest event ID we've seen at the beginning of the migration,
        # then scan all events greater than that ID and migrate the ones
        # where necessary. (This still isn't even guaranteed to catch all
        # of the events due to processing latency, but it's a better shot.)
        # Create a new destination group.
        destination = Group.objects.create(
            project_id=project.id,
            short_id=project.next_short_id(),
            **get_group_creation_attributes(events)
        )

        destination_id = destination.id

        # Move the group hashes to the destination.
        # TODO: What happens if this ``GroupHash`` has already been
        # migrated somewhere else? Right now, this just assumes we have
        # exclusive access to it (which is not a safe assumption.)
        GroupHash.objects.filter(
            project_id=project.id,
            hash__in=fingerprints,
        ).update(group=destination_id)

        # TODO: Create activity records for the source and destination group.
    else:
        # Update the existing destination group.
        destination = Group.objects.get(id=destination_id)
        destination.update(**get_group_backfill_attributes(destination, events))

    event_id_set = set(event.id for event in events)

    Event.objects.filter(
        project_id=project.id,
        id__in=event_id_set,
    ).update(group_id=destination_id)

    for event in events:
        event.group = destination

    EventTag.objects.filter(
        project_id=project.id,
        event_id__in=event_id_set,
    ).update(group_id=destination_id)

    event_event_id_set = set(event.event_id for event in events)

    EventMapping.objects.filter(
        project_id=project.id,
        event_id__in=event_event_id_set,
    ).update(group_id=destination_id)

    UserReport.objects.filter(
        project_id=project.id,
        event_id__in=event_event_id_set,
    ).update(group=destination_id)

    return destination.id


def truncate_denormalizations(group_id):
    GroupTagKey.objects.filter(
        group_id=group_id,
    ).delete()

    GroupTagValue.objects.filter(
        group_id=group_id,
    ).delete()

    GroupRelease.objects.filter(
        group_id=group_id,
    ).delete()

    tsdb.delete([
        tsdb.models.group,
    ], [group_id])

    tsdb.delete_distinct_counts([
        tsdb.models.users_affected_by_group,
    ], [group_id])

    tsdb.delete_frequencies([
        tsdb.models.frequent_releases_by_group,
        tsdb.models.frequent_environments_by_group,
    ], [group_id])


def collect_tag_data(events):
    results = {}

    for event in events:
        tags = results.setdefault((event.project_id, event.group_id), {})

        for key, value in event.get_tags():
            values = tags.setdefault(key, {})

            if value in values:
                times_seen, first_seen, last_seen = values[value]
                values[value] = (times_seen + 1, event.datetime, last_seen)
            else:
                values[value] = (0, event.datetime, event.datetime)

    return results


def repair_tag_data(events):
    # Repair `GroupTag{Key,Value}` data.
    for (project_id, group_id), keys in collect_tag_data(events).items():
        for key, values in keys.items():
            GroupTagKey.objects.get_or_create(
                project_id=project_id,
                group_id=group_id,
                key=key,
            )

            for value, (times_seen, first_seen, last_seen) in values.items():
                instance, created = GroupTagValue.objects.get_or_create(
                    project_id=project_id,
                    group=group_id,
                    key=key,
                    value=value,
                    defaults={
                        'first_seen': first_seen,
                        'last_seen': last_seen,
                        'times_seen': times_seen,
                    },
                )

                if not created:
                    instance.update(
                        first_seen=first_seen,
                        times_seen=F('times_seen') + times_seen,
                    )


def collect_release_data(events):
    raise NotImplementedError


def collect_tsdb_data(events):
    raise NotImplementedError


def repair_denormalizations(events):
    repair_tag_data(events)

    # TODO: Repair `GroupRelease` data.

    # TODO: Repair TSDB data.
    raise NotImplementedError


def unmerge(source_id, destination_id, fingerprints, cursor=None, batch_size=500):
    # XXX: If a ``GroupHash`` is unmerged *again* while this operation is
    # already in progress, some events from the fingerprint associated with the
    # hash may not be migrated to the new destination! We could solve this with
    # an exclusive lock on the ``GroupHash`` record (I think) as long as
    # nothing in the ``EventManager`` is going to try and preempt that. (I'm
    # not 100% sure that's the case.)

    # XXX: The queryset chunking logic below is awfully similar to
    # ``RangeQuerySetWrapper``. Ideally that could be refactored to be able to
    # be run without iteration by passing around a state object and we could
    # just use that here instead.

    if cursor is None:
        truncate_denormalizations(source_id)

    # We fetch the events in descending order by their primary key to get the
    # best approximation of the most recently received events.
    queryset = Event.objects.filter(group_id=source_id).order_by('-id')
    if cursor is not None:
        queryset = queryset.filter(id__lt=cursor)

    events = list(queryset[:batch_size])

    # If there are no more events to process, we're done with the migration.
    if not events:
        return destination_id

    Event.objects.bind_nodes(events, 'data')

    destination_id = migrate_events(
        source_id,
        destination_id,
        fingerprints,
        filter(
            lambda event: get_fingerprint(event) in fingerprints,
            events,
        )
    )

    repair_denormalizations(events)

    return unmerge(
        source_id,
        destination_id,
        fingerprints,
        cursor=events[-1].id,
        batch_size=batch_size,
    )
