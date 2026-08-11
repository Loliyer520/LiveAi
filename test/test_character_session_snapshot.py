import unittest

from core.character_session import CharacterSessionRegistry
from core.event_adapters import envelope_from_scope_turn_item
from core.events import ChatMessage


def item(text='hello'):
    return {
        'kind': 'message',
        'message': ChatMessage(
            chat_type='group', chat_id=7, user_id=1, text=text,
            raw_message=text, sender={'nickname': 'user'}, message_id=1,
            timestamp=1.0,
        ),
        'cleaned': text,
        'agent_id': 'agent-1',
        'scope_key': 'group:7',
        'trigger_messages': [{'text': text}],
    }


class CharacterSessionSnapshotTests(unittest.TestCase):
    def test_snapshot_is_read_only_and_reports_identity_and_counts(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        value = item()
        session.activate()
        session.append_event(envelope_from_scope_turn_item(value), transient=value)
        session.append_task({'task_id': 'task-1'})
        before = session.snapshot()
        after = session.snapshot()
        self.assertEqual(before, after)
        self.assertEqual(before.scope_type, 'group')
        self.assertEqual(before.scope_id, '7')
        self.assertEqual(before.scope_key, 'group:7')
        self.assertTrue(before.active)
        self.assertTrue(before.busy)
        self.assertEqual(before.pending_event_count, 1)
        self.assertEqual(before.pending_task_count, 1)
        self.assertFalse(before.retired)
        self.assertEqual(session.pending_event_count(), 1)
        self.assertEqual(session.pending_task_count(), 1)

    def test_clear_and_discard_snapshots_are_consistent(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        value = item()
        session.activate()
        session.append_event(envelope_from_scope_turn_item(value), transient=value)
        session.append_task({'task_id': 'task-1'})
        session.clear_runtime_state()
        cleared = session.snapshot()
        self.assertFalse(cleared.active)
        self.assertFalse(cleared.busy)
        self.assertEqual(cleared.pending_event_count, 0)
        self.assertEqual(cleared.pending_task_count, 0)
        self.assertFalse(cleared.retired)
        self.assertTrue(registry.discard_if_idle('group:7'))
        retired = session.snapshot()
        self.assertTrue(retired.retired)
        self.assertFalse(retired.active)
        self.assertEqual(registry.snapshots(), ())

    def test_registry_snapshots_are_sorted_and_do_not_create_sessions(self):
        registry = CharacterSessionRegistry()
        registry.get_or_create('private', '9')
        registry.get_or_create('group', '7')
        snapshots = registry.snapshots()
        self.assertEqual(
            tuple(snapshot.scope_key for snapshot in snapshots),
            ('group:7', 'private:9'),
        )
        self.assertEqual(registry.list_scope_keys(), ('group:7', 'private:9'))
        self.assertEqual(len(registry.snapshots()), 2)

    def test_snapshot_dict_has_no_mutator_or_owner_side_effect(self):
        registry = CharacterSessionRegistry()
        session = registry.get_or_create('group', '7')
        data = session.snapshot().to_dict()
        self.assertEqual(
            set(data),
            {
                'scope_type', 'scope_id', 'scope_key', 'active',
                'pending_event_count', 'pending_task_count', 'retired', 'busy',
            },
        )
        data['active'] = True
        self.assertFalse(session.is_active())


if __name__ == '__main__':
    unittest.main()
