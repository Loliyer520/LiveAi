import unittest

from core.character_session import CharacterSession, CharacterSessionRegistry, ScopeIdentity
from core.event_envelope import EventEnvelope, EventType
from core.event_mailbox import InMemoryEventMailbox


def event(scope_type, scope_id, text, event_id):
    return EventEnvelope(
        event_type=EventType.MESSAGE,
        scope_type=scope_type,
        scope_id=scope_id,
        payload={'text': text},
        source='test',
        event_id=event_id,
        occurred_at=1.0,
    )


class CharacterSessionTests(unittest.TestCase):
    def test_scope_identity_and_single_active_owner(self):
        session = CharacterSession(ScopeIdentity.from_scope_key('group:7'))
        self.assertEqual(session.scope_key, 'group:7')
        self.assertTrue(session.activate())
        self.assertFalse(session.activate())
        self.assertTrue(session.is_active())
        session.deactivate()
        self.assertFalse(session.is_active())

    def test_scope_isolation_fifo_and_transient_identity(self):
        mailbox = InMemoryEventMailbox()
        group = CharacterSession(ScopeIdentity('group', '7'), mailbox=mailbox)
        private = CharacterSession(ScopeIdentity('private', '9'), mailbox=mailbox)
        first, second = {'id': 1}, {'id': 2}
        group.append_event(event('group', '7', 'one', 'e1'), transient=first)
        group.append_event(event('group', '7', 'two', 'e2'), transient=second)
        private.append_event(event('private', '9', 'private', 'e3'), transient={'id': 3})
        self.assertIs(group.pop_raw_entry().transient, first)
        self.assertIs(group.pop_raw_entry().transient, second)
        self.assertEqual(private.pending_event_count(), 1)

    def test_message_before_task_promotion(self):
        session = CharacterSession(ScopeIdentity('group', '7'))
        session.append_task({'task_id': 'task-1'})
        session.append_event(event('group', '7', 'message', 'e1'), transient={'message': True})
        self.assertIsNone(session.promote_task_if_mailbox_empty())
        session.pop_raw_entry()
        self.assertEqual(session.promote_task_if_mailbox_empty()['task_id'], 'task-1')

    def test_clear_is_scope_local_with_shared_mailbox(self):
        mailbox = InMemoryEventMailbox()
        group = CharacterSession(ScopeIdentity('group', '7'), mailbox=mailbox)
        private = CharacterSession(ScopeIdentity('private', '9'), mailbox=mailbox)
        group.activate()
        group.append_task({'task_id': 'task-1'})
        group.append_event(event('group', '7', 'group', 'e1'))
        private.append_event(event('private', '9', 'private', 'e2'))
        group.clear_runtime_state()
        self.assertFalse(group.is_active())
        self.assertEqual(group.pending_event_count(), 0)
        self.assertEqual(group.pending_task_count(), 0)
        self.assertEqual(private.pending_event_count(), 1)

    def test_rejects_cross_scope_event(self):
        session = CharacterSession(ScopeIdentity('group', '7'))
        with self.assertRaises(ValueError):
            session.append_event(event('group', '8', 'wrong', 'e1'))


class CharacterSessionRegistryTests(unittest.TestCase):
    def test_pending_task_count_is_read_only_for_unknown_scope(self):
        registry = CharacterSessionRegistry()
        self.assertEqual(registry.pending_task_count('group:7'), 0)
        self.assertEqual(registry.list_scope_keys(), ())

    def test_pending_task_transition_ops_preserve_fifo_and_identity(self):
        registry = CharacterSessionRegistry()
        first = {'task_id': 'task-1'}
        second = {'task_id': 'task-2'}

        self.assertEqual(registry.append_pending_task('group:7', first), 0)
        self.assertEqual(registry.append_pending_task('group:7', second), 1)
        self.assertEqual(registry.pending_task_count('group:7'), 2)
        self.assertIs(
            registry.promote_pending_task_if_mailbox_empty('group:7'), first,
        )
        self.assertIs(
            registry.promote_pending_task_if_mailbox_empty('group:7'), second,
        )
        self.assertIsNone(
            registry.promote_pending_task_if_mailbox_empty('group:7')
        )

    def test_pending_task_transition_ops_preserve_message_priority_and_scope(self):
        registry = CharacterSessionRegistry()
        group_task = {'task_id': 'group-task'}
        private_task = {'task_id': 'private-task'}
        group = registry.get_or_create('group', '7')
        group.activate()
        group.append_event(event('group', '7', 'message', 'e1'))
        registry.append_pending_task('group:7', group_task)
        registry.append_pending_task('private:9', private_task)

        self.assertIsNone(
            registry.promote_pending_task_if_mailbox_empty('group:7')
        )
        self.assertIs(
            registry.promote_pending_task_if_mailbox_empty('private:9'),
            private_task,
        )
        registry.clear_pending_tasks('group:7')
        self.assertEqual(registry.pending_task_count('group:7'), 0)
        self.assertTrue(registry.is_active('group:7'))
        self.assertEqual(group.pending_event_count(), 1)

    def test_pop_tool_raw_unknown_scope_is_read_only(self):
        registry = CharacterSessionRegistry()
        self.assertIsNone(registry.pop_tool_raw('group:7'))
        self.assertEqual(registry.list_scope_keys(), ())

    def test_pop_tool_raw_preserves_fifo_identity_and_other_state(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        first = event('group', '7', 'message', 'e1')
        second = event('group', '7', 'report', 'e2')
        first_item = {'item_id': 'first'}
        second_item = {'item_id': 'second'}
        session.activate()
        session.append_event(first, transient=first_item)
        session.append_event(second, transient=second_item)
        registry.append_pending_task('group:7', {'task_id': 'task-1'})

        self.assertIs(registry.pop_tool_raw('group:7'), first_item)
        self.assertEqual(session.pending_event_count(), 1)
        self.assertIs(registry.pop_tool_raw('group:7'), second_item)
        self.assertIsNone(registry.pop_tool_raw('group:7'))
        self.assertTrue(registry.is_active('group:7'))
        self.assertEqual(registry.pending_task_count('group:7'), 1)

    def test_pop_tool_raw_rejects_missing_transient(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        session.append_event(event('group', '7', 'message', 'e1'))

        with self.assertRaisesRegex(
            RuntimeError, 'pending mailbox entry missing transient item: group:7'
        ):
            registry.pop_tool_raw('group:7')


    def test_discard_retires_old_reference_and_preserves_prior_empty_state(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        self.assertTrue(registry.discard_if_idle('group:7'))
        self.assertTrue(session.is_retired())
        with self.assertRaises(RuntimeError):
            session.activate()
        with self.assertRaises(RuntimeError):
            session.append_event(event('group', '7', 'old', 'old-event'), transient={'old': True})
        with self.assertRaises(RuntimeError):
            session.append_task({'task_id': 'old'})
        replacement = registry.get_or_create('group', '7')
        self.assertIsNot(replacement, session)
        self.assertFalse(replacement.is_retired())
        self.assertTrue(replacement.activate())

    def test_clear_runtime_state_is_not_retirement_and_preserves_identity(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        session.activate()
        session.append_event(event('group', '7', 'old', 'old-event'), transient={'event': True})
        session.append_task({'task_id': 'task-1'})
        session.clear_runtime_state()
        self.assertFalse(session.is_retired())
        self.assertTrue(session.activate())
        self.assertEqual(session.pending_event_count(), 0)
        self.assertEqual(session.pending_task_count(), 0)
    def test_same_scope_has_one_session_and_scopes_are_isolated(self):
        registry = CharacterSessionRegistry()
        first = registry.get_or_create('group', '7')
        same = registry.get_or_create('group', '7')
        other = registry.get_or_create('private', '7')
        self.assertIs(first, same)
        self.assertIsNot(first, other)
        self.assertEqual(registry.list_scope_keys(), ('group:7', 'private:7'))
        self.assertIs(first.mailbox, registry.mailbox)
        self.assertIs(other.mailbox, registry.mailbox)

    def test_discard_only_idle_and_clear_drops_runtime_state(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        session.activate()
        self.assertFalse(registry.discard_if_idle('group:7'))
        session.deactivate()
        session.append_task({'task_id': 'task-1'})
        self.assertFalse(registry.discard_if_idle('group:7'))
        session.promote_task_if_mailbox_empty()
        self.assertTrue(registry.discard_if_idle('group:7'))
        replacement = registry.get_or_create('group', '7')
        self.assertIsNot(replacement, session)
        replacement.append_event(event('group', '7', 'pending', 'e1'))
        registry.clear_runtime_state()
        self.assertEqual(registry.list_scope_keys(), ())
        self.assertTrue(registry.mailbox.is_empty())


if __name__ == '__main__':
    unittest.main()
