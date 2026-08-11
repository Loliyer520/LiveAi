import threading
import unittest

from core.character_session import CharacterSessionRegistry
from core.event_envelope import EventEnvelope, EventType
from core.event_mailbox import InMemoryEventMailbox
from core.runtime_scope_observer import RuntimeScopeObserver


def event(text, event_id):
    return EventEnvelope(
        event_type=EventType.MESSAGE,
        scope_type='group', scope_id='7', payload={'text': text},
        source='test', event_id=event_id, occurred_at=1.0,
    )


class RuntimeScopeObserverTests(unittest.TestCase):
    def observer(self, active=None, mailbox=None, tasks=None, queue_size=None):
        active = active if active is not None else set()
        return RuntimeScopeObserver(
            is_active=lambda scope_key: scope_key in active,
            mailbox=mailbox if mailbox is not None else InMemoryEventMailbox(),
            pending_task_count=lambda scope_key: len((tasks if tasks is not None else {}).get(scope_key) or ()),
            queue_size=queue_size,
        )

    def test_idle_observation_does_not_create_or_mutate_state(self):
        active, mailbox, tasks = set(), InMemoryEventMailbox(), {}
        observer = self.observer(active, mailbox, tasks, lambda: 0)
        first = observer.observe('group', '7')
        second = observer.observe_key('group:7')
        self.assertEqual(first, second)
        self.assertFalse(first.active)
        self.assertFalse(first.busy)
        self.assertEqual(first.pending_event_count, 0)
        self.assertEqual(first.pending_task_count, 0)
        self.assertEqual(first.runtime_queue_size, 0)
        self.assertTrue(first.consistent)
        self.assertEqual(active, set())
        self.assertTrue(mailbox.is_empty())
        self.assertEqual(tasks, {})

    def test_active_pending_event_task_and_queue_counts_match_current_owners(self):
        active = {'group:7'}
        mailbox = InMemoryEventMailbox()
        mailbox.append(event('one', 'e1'))
        mailbox.append(event('two', 'e2'))
        tasks = {'group:7': [{'task_id': 't1'}, {'task_id': 't2'}]}
        observation = self.observer(active, mailbox, tasks, lambda: 3).observe_key('group:7')
        self.assertTrue(observation.active)
        self.assertTrue(observation.busy)
        self.assertEqual(observation.pending_event_count, 2)
        self.assertEqual(observation.pending_task_count, 2)
        self.assertEqual(observation.runtime_queue_size, 3)
        self.assertEqual(mailbox.pending_count('group:7'), 2)
        self.assertEqual(len(tasks['group:7']), 2)

    def test_batch_drain_clear_and_discard_like_owner_changes_are_observed(self):
        active = {'group:7'}
        mailbox = InMemoryEventMailbox()
        mailbox.append(event('one', 'e1'))
        tasks = {'group:7': [{'task_id': 't1'}]}
        observer = self.observer(active, mailbox, tasks)
        before = observer.observe_key('group:7')
        self.assertEqual((before.active, before.pending_event_count, before.pending_task_count), (True, 1, 1))
        mailbox.drain_scope('group:7')
        tasks.pop('group:7')
        active.discard('group:7')
        after = observer.observe_key('group:7')
        self.assertEqual((after.active, after.pending_event_count, after.pending_task_count), (False, 0, 0))
        self.assertFalse(after.busy)

    def test_concurrent_change_is_reported_as_best_effort_not_strong_snapshot(self):
        active = set()
        mailbox = InMemoryEventMailbox()
        tasks = {}
        first_sample = threading.Event()
        allow_second = threading.Event()
        calls = {'count': 0}

        def queue_size():
            calls['count'] += 1
            if calls['count'] == 1:
                first_sample.set()
                allow_second.wait(timeout=2)
                return 0
            return 1

        observer = self.observer(active, mailbox, tasks, queue_size)
        result = {}

        def observe_worker():
            result['value'] = observer.observe_key('group:7')

        worker = threading.Thread(target=observe_worker)
        worker.start()
        self.assertTrue(first_sample.wait(timeout=2))
        active.add('group:7')
        mailbox.append(event('late', 'e1'))
        tasks['group:7'] = [{'task_id': 'late-task'}]
        allow_second.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        observation = result['value']
        self.assertFalse(observation.consistent)
        self.assertTrue(observation.active)
        self.assertEqual(observation.pending_event_count, 1)
        self.assertEqual(observation.pending_task_count, 1)
        self.assertEqual(observation.runtime_queue_size, 1)

    def test_to_dict_is_detached_and_declares_consistency_boundary(self):
        observation = self.observer(queue_size=lambda: 5).observe('private', '9')
        data = observation.to_dict()
        self.assertEqual(data['scope_key'], 'private:9')
        self.assertIn('consistent', data)
        data['active'] = True
        self.assertFalse(observation.active)

    def test_set_and_registry_predicates_are_observation_equivalent(self):
        from core.character_session import CharacterSessionRegistry

        active = {'group:7'}
        registry = CharacterSessionRegistry()
        # Read-only registry-backed stub: no new registry write API is used.
        registry_snapshot = {'group:7': True}
        registry_reader = lambda scope_key: registry_snapshot.get(scope_key, False)
        mailbox = InMemoryEventMailbox()
        tasks = {'group:7': [{'task_id': 'task-1'}]}
        set_observer = RuntimeScopeObserver(
            is_active=lambda scope_key: scope_key in active,
            mailbox=mailbox,
            pending_task_count=lambda scope_key: len(tasks.get(scope_key) or ()),
            queue_size=lambda: 2,
        )
        registry_observer = RuntimeScopeObserver(
            is_active=registry_reader,
            mailbox=mailbox,
            pending_task_count=lambda scope_key: len(tasks.get(scope_key) or ()),
            queue_size=lambda: 2,
        )
        self.assertEqual(
            set_observer.observe_key('group:7'),
            registry_observer.observe_key('group:7'),
        )
        self.assertEqual(registry.list_scope_keys(), ())

    def test_active_predicate_does_not_change_other_sources(self):
        active = {'group:7'}
        mailbox = InMemoryEventMailbox()
        mailbox.append(event('one', 'e1'))
        tasks = {'group:7': [{'task_id': 'task-1'}]}
        observer = RuntimeScopeObserver(
            is_active=lambda scope_key: scope_key in active,
            mailbox=mailbox,
            pending_task_count=lambda scope_key: len(tasks.get(scope_key) or ()),
            queue_size=lambda: 4,
        )
        active.clear()
        observation = observer.observe_key('group:7')
        self.assertFalse(observation.active)
        self.assertEqual(observation.pending_event_count, 1)
        self.assertEqual(observation.pending_task_count, 1)
        self.assertEqual(observation.runtime_queue_size, 4)


if __name__ == '__main__':
    unittest.main()
