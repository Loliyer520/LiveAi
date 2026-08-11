import unittest

from core.ai_runtime import AIOrchestrator
from core.character_session import CharacterSessionRegistry


class CharacterSessionShadowIntegrationTests(unittest.TestCase):
    def runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._character_sessions = CharacterSessionRegistry()
        return runtime

    def test_public_shadow_lookup_is_stable_per_scope(self):
        runtime = self.runtime()
        first = runtime.get_character_session('group', '7')
        same = runtime.get_character_session_by_key('group:7')
        other = runtime.get_character_session('private', '7')
        self.assertIs(first, same)
        self.assertIsNot(first, other)
        self.assertEqual(first.scope_key, 'group:7')

    def test_shadow_lookup_does_not_create_runtime_owner_fields(self):
        runtime = self.runtime()
        session = runtime.get_character_session('group', '7')
        self.assertFalse(session.is_active())
        self.assertEqual(session.pending_event_count(), 0)
        self.assertFalse(hasattr(runtime, '_active_scope_turns'))
        self.assertFalse(hasattr(runtime, '_event_mailbox'))
        self.assertFalse(hasattr(runtime, '_pending_scope_tasks'))

    def test_invalid_scope_key_is_rejected(self):
        runtime = self.runtime()
        with self.assertRaises(ValueError):
            runtime.get_character_session_by_key('invalid')

    def test_snapshot_observation_does_not_change_runtime_status_contract(self):
        runtime = self.runtime()
        runtime.get_character_session('group', '7')
        before_keys = set(AIOrchestrator.get_runtime_status.__code__.co_consts)
        snapshots = runtime.get_character_session_snapshots()
        after_keys = set(AIOrchestrator.get_runtime_status.__code__.co_consts)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]['scope_key'], 'group:7')
        self.assertEqual(before_keys, after_keys)
        self.assertNotIn('character_sessions', AIOrchestrator.get_runtime_status.__code__.co_consts)


if __name__ == '__main__':
    unittest.main()
