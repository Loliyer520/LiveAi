import unittest

from core.event_adapters import envelope_from_scope_turn_item
from core.event_envelope import EventType
from core.timed_event_messages import TimedEventMessageFactory


class TimedEventMessageFactoryTests(unittest.TestCase):
    def setUp(self):
        self.factory = TimedEventMessageFactory()

    def test_alarm_message_uses_scope_and_alarm_metadata(self):
        message = self.factory.build_alarm_message(
            {'scope_type': 'group', 'scope_id': '7'},
            task_id='alarm-1',
            text='闹钟响啦：喝水',
            occurred_at=123.5,
        )

        self.assertEqual(message.chat_type, 'group')
        self.assertEqual(message.chat_id, 7)
        self.assertEqual(message.text, '闹钟响啦：喝水')
        self.assertEqual(message.timestamp, 123.5)
        self.assertEqual(message.raw_data, {
            'source': 'alarm',
            'system_event': 'alarm',
            'task_id': 'alarm-1',
        })
        envelope = envelope_from_scope_turn_item({
            'kind': 'message',
            'message': message,
        })
        self.assertEqual(envelope.event_type, EventType.ALARM)
        self.assertEqual(envelope.scope_key, 'group:7')

    def test_recurring_message_uses_creator_scope_fallback_and_metadata(self):
        message = self.factory.build_recurring_message({
            'id': 'recurring-1',
            'creator_scope': 'private:9',
            'instruction': '检查构建状态',
        }, occurred_at=124.0)

        self.assertEqual(message.chat_type, 'private')
        self.assertEqual(message.chat_id, 9)
        self.assertEqual(message.text, '[循环任务触发] 检查构建状态')
        self.assertEqual(message.timestamp, 124.0)
        self.assertEqual(message.raw_data, {
            'source': 'recurring_task',
            'system_event': 'recurring_task',
            'task_id': 'recurring-1',
        })
        envelope = envelope_from_scope_turn_item({
            'kind': 'message',
            'message': message,
        })
        self.assertEqual(envelope.event_type, EventType.RECURRING_TASK)
        self.assertEqual(envelope.scope_key, 'private:9')

    def test_master_scope_normalizes_chat_id_to_zero(self):
        alarm = self.factory.build_alarm_message(
            {'scope_type': 'master', 'scope_id': 'global'},
            task_id='alarm-master',
            text='wake',
        )
        recurring = self.factory.build_recurring_message({
            'id': 'recurring-master',
            'target_scope': 'master:global',
            'instruction': 'run',
        })

        self.assertEqual(alarm.chat_id, 0)
        self.assertEqual(recurring.chat_id, 0)

    def test_invalid_scopes_return_none(self):
        invalid_alarm_payloads = (
            {},
            {'scope_type': 'group'},
            {'scope_type': 'group', 'scope_id': 'bad'},
        )
        for payload in invalid_alarm_payloads:
            with self.subTest(payload=payload):
                self.assertIsNone(self.factory.build_alarm_message(
                    payload,
                    task_id='alarm-1',
                    text='wake',
                ))

        for task in (
            {},
            {'target_scope': 'group'},
            {'target_scope': 'group:bad'},
        ):
            with self.subTest(task=task):
                self.assertIsNone(self.factory.build_recurring_message(task))


if __name__ == '__main__':
    unittest.main()
