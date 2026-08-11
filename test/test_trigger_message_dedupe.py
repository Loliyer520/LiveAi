import unittest

from core.ai_runtime import AIOrchestrator


class TriggerMessageDedupeTests(unittest.TestCase):
    def test_dedupe_trigger_message_entries_keeps_first_unique_entry(self):
        runtime = object.__new__(AIOrchestrator)
        entries = [
            {
                'message_id': 1001,
                'message_ref': 'A1B2',
                'raw_message': '你好',
                'text': '你好',
                'timestamp': 1,
                'source_label': 'QQ群消息',
            },
            {
                'message_id': 1001,
                'message_ref': 'A1B2',
                'raw_message': '你好',
                'text': '你好',
                'timestamp': 1,
                'source_label': 'QQ群消息',
            },
            {
                'message_id': 1002,
                'message_ref': 'A1B3',
                'raw_message': '还有吗',
                'text': '还有吗',
                'timestamp': 2,
                'source_label': 'QQ群消息',
            },
        ]

        result = AIOrchestrator._dedupe_trigger_message_entries(runtime, entries)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['message_id'], 1001)
        self.assertEqual(result[1]['message_id'], 1002)


if __name__ == '__main__':
    unittest.main()
