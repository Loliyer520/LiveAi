import threading
import unittest

from core.character_session import CharacterSessionRegistry
from core.event_adapters import envelope_from_scope_turn_item
from core.events import ChatMessage


def item(index):
    return {
        'kind': 'message',
        'message': ChatMessage(
            chat_type='group', chat_id=7, user_id=index, text=str(index),
            raw_message=str(index), sender={'nickname': str(index)},
            message_id=index, timestamp=float(index),
        ),
        'cleaned': str(index),
        'agent_id': 'agent-1',
        'scope_key': 'group:7',
        'trigger_messages': [{'text': str(index)}],
    }


class CharacterSessionConcurrencyTests(unittest.TestCase):
    def test_registry_get_or_create_is_single_identity_under_concurrency(self):
        registry = CharacterSessionRegistry()
        barrier = threading.Barrier(16)
        sessions = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            session = registry.get_or_create('group', '7')
            with lock:
                sessions.append(session)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len({id(session) for session in sessions}), 1)

    def test_append_and_discard_race_never_discards_busy_session(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        session.activate()
        barrier = threading.Barrier(2)
        discarded = []

        def append_worker():
            barrier.wait()
            value = item(1)
            session.append_event(envelope_from_scope_turn_item(value), transient=value)

        def discard_worker():
            barrier.wait()
            discarded.append(registry.discard_if_idle('group:7'))

        first = threading.Thread(target=append_worker)
        second = threading.Thread(target=discard_worker)
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(discarded, [False])
        self.assertIs(registry.get('group:7'), session)
        self.assertEqual(session.pending_event_count(), 1)

    def test_clear_racing_with_append_has_no_deadlock_and_new_instance_is_clean(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        barrier = threading.Barrier(2)

        def append_worker():
            barrier.wait()
            for index in range(1, 101):
                value = item(index)
                session.append_event(envelope_from_scope_turn_item(value), transient=value)

        def clear_worker():
            barrier.wait()
            registry.clear_runtime_state()

        first = threading.Thread(target=append_worker)
        second = threading.Thread(target=clear_worker)
        first.start()
        second.start()
        first.join(timeout=3)
        second.join(timeout=3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        replacement = registry.get_or_create('group', '7')
        self.assertIsNot(replacement, session)
        self.assertFalse(replacement.is_active())
        self.assertEqual(replacement.pending_task_count(), 0)

    def test_idle_discard_checks_tasks_events_and_active_state(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        session.activate()
        self.assertFalse(registry.discard_if_idle('group:7'))
        session.deactivate()
        value = item(1)
        session.append_event(envelope_from_scope_turn_item(value), transient=value)
        self.assertFalse(registry.discard_if_idle('group:7'))
        session.pop_raw_entry()
        session.append_task({'task_id': 'task-1'})
        self.assertFalse(registry.discard_if_idle('group:7'))
        session.promote_task_if_mailbox_empty()
        self.assertTrue(registry.discard_if_idle('group:7'))

    def test_successful_discard_retires_stale_reference(self):
        registry = CharacterSessionRegistry()
        stale_session = registry.get_or_create('group', '7')
        self.assertTrue(registry.discard_if_idle('group:7'))
        self.assertTrue(stale_session.is_retired())
        value = item(1)
        with self.assertRaises(RuntimeError):
            stale_session.activate()
        with self.assertRaises(RuntimeError):
            stale_session.append_event(
                envelope_from_scope_turn_item(value), transient=value,
            )
        with self.assertRaises(RuntimeError):
            stale_session.append_task({'task_id': 'late-task'})
        replacement = registry.get_or_create('group', '7')
        self.assertIsNot(replacement, stale_session)
        self.assertFalse(replacement.is_retired())

    def test_discard_race_preserves_every_accepted_event(self):
        for index in range(1, 101):
            registry = CharacterSessionRegistry()
            stale_session = registry.get_or_create('group', '7')
            value = item(index)
            barrier = threading.Barrier(2)
            outcome = {}

            def append_worker():
                barrier.wait()
                try:
                    stale_session.append_event(
                        envelope_from_scope_turn_item(value), transient=value,
                    )
                    outcome['accepted'] = True
                except RuntimeError:
                    outcome['accepted'] = False

            def discard_worker():
                barrier.wait()
                outcome['discarded'] = registry.discard_if_idle('group:7')

            first = threading.Thread(target=append_worker)
            second = threading.Thread(target=discard_worker)
            first.start()
            second.start()
            first.join(timeout=2)
            second.join(timeout=2)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())

            if outcome['accepted']:
                self.assertFalse(outcome['discarded'])
                self.assertIs(registry.get('group:7'), stale_session)
                self.assertEqual(stale_session.pending_event_count(), 1)
            else:
                self.assertTrue(outcome['discarded'])
                self.assertTrue(stale_session.is_retired())
                self.assertIsNone(registry.get('group:7'))

    def test_cancellation_is_explicit_clear_not_retirement(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        session.activate()
        value = item(1)
        session.append_event(envelope_from_scope_turn_item(value), transient=value)
        session.append_task({'task_id': 'task-1'})
        session.clear_runtime_state()
        self.assertFalse(session.is_active())
        self.assertFalse(session.is_retired())
        self.assertEqual(session.pending_event_count(), 0)
        self.assertEqual(session.pending_task_count(), 0)
        self.assertTrue(session.activate())
        self.assertFalse(registry.discard_if_idle('group:7'))
        session.deactivate()
        self.assertTrue(registry.discard_if_idle('group:7'))


    def test_discard_race_100_rounds_accept_or_retire_without_loss(self):
        for index in range(100):
            registry = CharacterSessionRegistry()
            session = registry.get_or_create('group', '7')
            barrier = threading.Barrier(2)
            outcome = {}
            value = item(index)

            def append_worker():
                barrier.wait()
                try:
                    session.append_event(
                        envelope_from_scope_turn_item(value), transient=value,
                    )
                    outcome['accepted'] = True
                except RuntimeError:
                    outcome['accepted'] = False

            def discard_worker():
                barrier.wait()
                outcome['discarded'] = registry.discard_if_idle('group:7')

            first = threading.Thread(target=append_worker)
            second = threading.Thread(target=discard_worker)
            first.start()
            second.start()
            first.join(timeout=2)
            second.join(timeout=2)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            if outcome['accepted']:
                self.assertFalse(outcome['discarded'])
                self.assertIs(registry.get('group:7'), session)
                self.assertEqual(session.pending_event_count(), 1)
            else:
                self.assertTrue(outcome['discarded'])

                self.assertTrue(session.is_retired())
                self.assertIsNone(registry.get('group:7'))


if __name__ == '__main__':
    unittest.main()
