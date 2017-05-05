from __future__ import absolute_import

import itertools
import logging
import uuid
from datetime import datetime, timedelta

import pytz
from collections import OrderedDict

from sentry.event_manager import ScoreClause
from sentry.models import Environment, EnvironmentProject, Event, EventMapping, Group, GroupHash, GroupRelease, GroupTagKey, GroupTagValue, UserReport
from sentry.testutils import TestCase
from sentry.tasks.unmerge import get_fingerprint, unmerge, get_group_creation_attributes, get_group_backfill_attributes
from sentry.utils.dates import to_timestamp


class UnmergeTestCase(TestCase):
    def test_get_group_creation_attributes(self):
        now = datetime(2017, 5, 3, 6, 6, 6, tzinfo=pytz.utc)
        events = [
            Event(
                platform='javascript',
                message='Hello from JavaScript',
                datetime=now,
                data={
                    'type': 'default',
                    'metadata': {},
                    'tags': [
                        ['level', 'info'],
                        ['logger', 'javascript'],
                    ],
                },
            ),
            Event(
                platform='python',
                message='Hello from Python',
                datetime=now - timedelta(hours=1),
                data={
                    'type': 'default',
                    'metadata': {},
                    'tags': [
                        ['level', 'error'],
                        ['logger', 'python'],
                    ],
                },
            ),
            Event(
                platform='java',
                message='Hello from Java',
                datetime=now - timedelta(hours=2),
                data={
                    'type': 'default',
                    'metadata': {},
                    'tags': [
                        ['level', 'debug'],
                        ['logger', 'java'],
                    ],
                },
            ),
        ]

        assert get_group_creation_attributes(events) == {
            'active_at': now - timedelta(hours=2),
            'first_seen': now - timedelta(hours=2),
            'last_seen': now,
            'platform': 'java',
            'message': 'Hello from JavaScript',
            'level': logging.INFO,
            'score': ScoreClause.calculate(3, now),
            'logger': 'java',
            'times_seen': 3,
            'first_release': None,
            'culprit': '',
            'data': {
                'type': 'default',
                'last_received': to_timestamp(now),
                'metadata': {},
            },
        }

    def test_get_group_backfill_attributes(self):
        now = datetime(2017, 5, 3, 6, 6, 6, tzinfo=pytz.utc)
        assert get_group_backfill_attributes(
            Group(
                active_at=now,
                first_seen=now,
                last_seen=now,
                platform='javascript',
                message='Hello from JavaScript',
                level=logging.INFO,
                score=ScoreClause.calculate(3, now),
                logger='javascript',
                times_seen=1,
                first_release=None,
                culprit='',
                data={
                    'type': 'default',
                    'last_received': to_timestamp(now),
                    'metadata': {},
                },
            ),
            [
                Event(
                    platform='python',
                    message='Hello from Python',
                    datetime=now - timedelta(hours=1),
                    data={
                        'type': 'default',
                        'metadata': {},
                        'tags': [
                            ['level', 'error'],
                            ['logger', 'python'],
                        ],
                    },
                ),
                Event(
                    platform='java',
                    message='Hello from Java',
                    datetime=now - timedelta(hours=2),
                    data={
                        'type': 'default',
                        'metadata': {},
                        'tags': [
                            ['level', 'debug'],
                            ['logger', 'java'],
                        ],
                    },
                ),
            ],
        ) == {
            'active_at': now - timedelta(hours=2),
            'first_seen': now - timedelta(hours=2),
            'platform': 'java',
            'score': ScoreClause.calculate(3, now),
            'logger': 'java',
            'times_seen': 3,
            'first_release': None,
        }

    def test_unmerge(self):
        project = self.create_project()
        source = self.create_group(project)

        sequence = itertools.count(0)
        tag_values = itertools.cycle(['red', 'green', 'blue'])

        EnvironmentProject.objects.create(
            environment=Environment.objects.create(
                organization_id=project.organization_id,
                name='production',
            ),
            project=project,
        )

        def create_message_event(template, parameters):
            event_id = uuid.UUID(
                fields=(
                    next(sequence),
                    0x0,
                    0x1000,
                    0x80,
                    0x80,
                    0x808080808080,
                ),
            ).hex

            event = Event.objects.create(
                project_id=project.id,
                group_id=source.id,
                event_id=event_id,
                message='%s' % (id,),
                data={
                    'environment': 'production',
                    'type': 'default',
                    'metadata': {
                        'title': template % parameters,
                    },
                    'sentry.interfaces.Message': {
                        'message': template,
                        'params': parameters,
                        'formatted': template % parameters,
                    },
                    'tags': [
                        ['color', next(tag_values)],
                        ['environment', 'production'],
                        ['sentry:release', 'version'],
                    ],
                },
            )

            with self.tasks():
                Group.objects.add_tags(
                    source,
                    tags=event.get_tags(),
                )

            EventMapping.objects.create(
                project_id=project.id,
                group_id=source.id,
                event_id=event_id,
                date_added=event.datetime,
            )

            UserReport.objects.create(
                project_id=project.id,
                group_id=source.id,
                event_id=event_id,
                name='Log Hat',
                email='ceo@corptron.com',
                comments='Quack',
            )

            return event

        events = OrderedDict()

        for event in (create_message_event('This is message #%s.', i) for i in xrange(10)):
            events.setdefault(get_fingerprint(event), []).append(event)

        for event in (create_message_event('This is message #%s!', i) for i in xrange(10, 17)):
            events.setdefault(get_fingerprint(event), []).append(event)

        assert len(events) == 2
        assert sum(map(len, events.values())) == 17

        # XXX: This is super contrived considering that it doesn't actually go
        # through the event pipeline, but them's the breaks, eh?
        for fingerprint in events.keys():
            GroupHash.objects.create(
                project=project,
                group=source,
                hash=fingerprint,
            )

        # TODO: Check first, last seen.
        assert set(GroupTagKey.objects.filter(group=source).values_list('key', 'values_seen')) == set([
            (u'color', 3),
            (u'environment', 1),
            (u'sentry:release', 1),
        ])

        assert set(GroupTagValue.objects.filter(group=source).values_list('key', 'value', 'times_seen')) == set([
            (u'color', u'red', 6),
            (u'color', u'green', 6),
            (u'color', u'blue', 5),
            (u'environment', u'production', 17),
            (u'sentry:release', u'version', 17),
        ])

        destination = Group.objects.get(
            id=unmerge(
                source.project_id,
                source.id,
                None,
                [events.keys()[1]],
                batch_size=5,
            ),
        )

        assert source.id != destination.id
        assert source.project == destination.project

        source_event_event_ids = map(
            lambda event: event.event_id,
            events.values()[0],
        )

        assert source.event_set.count() == 10

        assert set(
            EventMapping.objects.filter(
                group_id=source.id,
            ).values_list('event_id', flat=True)
        ) == set(source_event_event_ids)

        assert set(
            UserReport.objects.filter(
                group_id=source.id,
            ).values_list('event_id', flat=True)
        ) == set(source_event_event_ids)

        assert set(
            GroupHash.objects.filter(
                group_id=source.id,
            ).values_list('hash', flat=True)
        ) == set([
            events.keys()[0]
        ])

        assert set(
            GroupRelease.objects.filter(
                group_id=source.id,
            ).values_list('environment', flat=True)
        ) == set([
            u'production',
        ])

        # TODO: Check first, last seen.
        assert set(GroupTagKey.objects.filter(group=source).values_list('key', 'values_seen')) == set([
            (u'color', 3),
            (u'environment', 1),
            (u'sentry:release', 1),
        ])

        assert set(GroupTagValue.objects.filter(group=source).values_list('key', 'value', 'times_seen')) == set([
            (u'color', u'red', 4),
            (u'color', u'green', 3),
            (u'color', u'blue', 3),
            (u'environment', u'production', 10),
            (u'sentry:release', u'version', 10),
        ])

        destination_event_event_ids = map(
            lambda event: event.event_id,
            events.values()[1],
        )

        assert destination.event_set.count() == 7

        assert set(
            EventMapping.objects.filter(
                group_id=destination.id,
            ).values_list('event_id', flat=True)
        ) == set(destination_event_event_ids)

        assert set(
            UserReport.objects.filter(
                group_id=destination.id,
            ).values_list('event_id', flat=True)
        ) == set(destination_event_event_ids)

        assert set(
            GroupHash.objects.filter(
                group_id=destination.id,
            ).values_list('hash', flat=True)
        ) == set([
            events.keys()[1]
        ])

        assert set(
            GroupRelease.objects.filter(
                group_id=destination.id,
            ).values_list('environment', flat=True)
        ) == set([
            u'production',
        ])

        # TODO: Check first, last seen.
        assert set(GroupTagKey.objects.filter(group=destination).values_list('key', 'values_seen')) == set([
            (u'color', 3),
            (u'environment', 1),
            (u'sentry:release', 1),
        ])

        assert set(GroupTagValue.objects.filter(group=destination).values_list('key', 'value', 'times_seen')) == set([
            (u'color', u'red', 2),
            (u'color', u'green', 3),
            (u'color', u'blue', 2),
            (u'environment', u'production', 7),
            (u'sentry:release', u'version', 7),
        ])
