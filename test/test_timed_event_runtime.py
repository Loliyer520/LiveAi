import asyncio
import time
import unittest
from unittest.mock import Mock, patch

from core.ai_runtime import AIOrchestrator
from core.event_adapters import envelope_from_scope_turn_item
from core.event_envelope import EventType


class TimedEventRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_alarm_runner_submits_system_message_and_updates_task(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.repo = Mock()
        runtime.repo.get_task.return_value = {
            'task_id': 'alarm-1',
            'payload': {
                'scope_type': 'group',
                'scope_id': '7',
                'note': '喝水',
            },
        }
        runtime._scheduled_alarm_ids = {'alarm-1'}
        runtime._submit_message = Mock()

        with patch('core.ai_runtime.asyncio.sleep', new=unittest.mock.AsyncMock()):
            await runtime._alarm_runner('alarm-1', time.time())

        message = runtime._submit_message.call_args.args[0]
        envelope = envelope_from_scope_turn_item({
            'kind': 'message',
            'message': message,
        })
        self.assertEqual(envelope.event_type, EventType.ALARM)
        self.assertEqual(envelope.scope_key, 'group:7')
        runtime.repo.add_note.assert_called_once_with(
            'group',
            '7',
            '闹钟已触发: 喝水',
        )
        runtime.repo.update_task.assert_called_once_with(
            'alarm-1',
            'done',
            '闹钟已触发: 喝水',
        )
        self.assertNotIn('alarm-1', runtime._scheduled_alarm_ids)

    async def test_alarm_runner_with_invalid_scope_finishes_without_submission(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.repo = Mock()
        runtime.repo.get_task.return_value = {
            'task_id': 'alarm-2',
            'payload': {'note': '无目标'},
        }
        runtime._scheduled_alarm_ids = {'alarm-2'}
        runtime._submit_message = Mock()

        with patch('core.ai_runtime.asyncio.sleep', new=unittest.mock.AsyncMock()):
            await runtime._alarm_runner('alarm-2', time.time())

        runtime._submit_message.assert_not_called()
        runtime.repo.add_note.assert_not_called()
        runtime.repo.update_task.assert_called_once_with(
            'alarm-2',
            'done',
            '闹钟已触发: 无目标',
        )

    def test_recurring_trigger_submits_classified_system_message(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._submit_message = Mock()

        runtime._trigger_recurring_task({
            'id': 'recurring-1',
            'target_scope': 'private:9',
            'instruction': '检查任务',
        })

        message = runtime._submit_message.call_args.args[0]
        envelope = envelope_from_scope_turn_item({
            'kind': 'message',
            'message': message,
        })
        self.assertEqual(envelope.event_type, EventType.RECURRING_TASK)
        self.assertEqual(envelope.scope_key, 'private:9')


if __name__ == '__main__':
    unittest.main()
