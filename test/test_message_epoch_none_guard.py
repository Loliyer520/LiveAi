import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_runtime import AIOrchestrator


class MessageEpochNoneGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_message_epoch_falls_back_to_current_when_none(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._message_epoch = 7

        self.assertEqual(runtime._resolve_message_epoch(None), 7)
        self.assertEqual(runtime._resolve_message_epoch(''), 7)
        self.assertEqual(runtime._resolve_message_epoch(3), 3)

    def test_reserve_task_scope_uses_current_epoch_when_pending_item_has_none(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._message_epoch = 11
        runtime._scope_turn_is_active = lambda _scope_key: True
        captured = []
        runtime._character_sessions = SimpleNamespace(
            append_pending_task=lambda scope_key, item: captured.append((scope_key, item))
        )

        reserved = runtime._reserve_task_scope(
            'private:1',
            {'task_id': 'task-1', 'message_epoch': None},
        )

        self.assertFalse(reserved)
        self.assertEqual(
            captured,
            [('private:1', {'kind': 'task', 'task_id': 'task-1', 'message_epoch': 11})],
        )

    async def test_process_task_accepts_none_message_epoch(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._message_epoch = 5
        runtime.repo = Mock()
        runtime.repo.get_task.return_value = None
        runtime._is_epoch_stale = lambda epoch: epoch != runtime._message_epoch

        await runtime._process_task({'task_id': 'task-1', 'message_epoch': None})

        runtime.repo.get_task.assert_called_once_with('task-1')


if __name__ == '__main__':
    unittest.main()
