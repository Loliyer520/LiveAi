import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_runtime import AIOrchestrator


class MessageRefTests(unittest.IsolatedAsyncioTestCase):
    def _bind_runtime_helpers(self, runtime):
        runtime._scope_key = AIOrchestrator._scope_key.__get__(runtime, AIOrchestrator)
        runtime._normalize_message_ref = AIOrchestrator._normalize_message_ref.__get__(runtime, AIOrchestrator)
        runtime._message_ref_alphabet = AIOrchestrator._message_ref_alphabet.__get__(runtime, AIOrchestrator)
        runtime._encode_message_ref_number = AIOrchestrator._encode_message_ref_number.__get__(runtime, AIOrchestrator)
        runtime._compute_message_ref = AIOrchestrator._compute_message_ref.__get__(runtime, AIOrchestrator)
        runtime._extract_image_refs = AIOrchestrator._extract_image_refs.__get__(runtime, AIOrchestrator)
        runtime._annotate_message_refs = AIOrchestrator._annotate_message_refs.__get__(runtime, AIOrchestrator)
        runtime._lookup_message_ref = AIOrchestrator._lookup_message_ref.__get__(runtime, AIOrchestrator)
        runtime._register_turn_message_ref = AIOrchestrator._register_turn_message_ref.__get__(runtime, AIOrchestrator)
        runtime._register_persistent_message_ref = AIOrchestrator._register_persistent_message_ref.__get__(runtime, AIOrchestrator)
        runtime._turn_message_ref_maps = {}
        runtime._turn_image_refs = {}
        return runtime

    def test_send_text_lines_prefixes_reply_cq_only_once(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(
            send_text=Mock(side_effect=[
                {'data': {'message_id': 'm1'}},
                {'data': {'message_id': 'm2'}},
            ])
        )
        runtime._split_long_reply_lines = lambda text: text

        entries = AIOrchestrator._send_text_lines(
            runtime,
            SimpleNamespace(chat_type='group', chat_id=7),
            '第一行\n第二行',
            reply_to_message_id='12345',
        )

        self.assertEqual(
            runtime.bot.send_text.call_args_list[0].args,
            ('group', 7, '[CQ:reply,id=12345]第一行'),
        )
        self.assertEqual(
            runtime.bot.send_text.call_args_list[1].args,
            ('group', 7, '第二行'),
        )
        self.assertEqual(entries[0]['reply_to_message_id'], '12345')
        self.assertEqual(entries[0]['raw_message'], '[CQ:reply,id=12345]第一行')
        self.assertEqual(entries[1]['raw_message'], '第二行')

    def test_recall_message_accepts_message_ref(self):
        runtime = self._bind_runtime_helpers(object.__new__(AIOrchestrator))
        runtime.repo = SimpleNamespace(list_messages=lambda *_args, **_kwargs: [])
        runtime.bot = SimpleNamespace(recall_message=Mock())

        scope_key = runtime._scope_key('group', '7')
        runtime._turn_message_ref_maps[scope_key] = {
            'AB12': {'message_id': '9001', 'message_ref': 'AB12', 'image_refs': []},
        }

        sent_entries = [{'text': 'hello', 'message_id': '9001', 'message_ref': 'AB12'}]
        checkpointed = [{'text': 'hello', 'message_id': '9001', 'message_ref': 'AB12'}]
        result = AIOrchestrator._execute_live_action_tool_call(
            runtime,
            'group',
            '7',
            'agent-1',
            SimpleNamespace(chat_type='group', chat_id=7),
            SimpleNamespace(name='recall_message', input={'message_ref': 'ab12'}),
            None,
            True,
            True,
            sent_entries,
            checkpointed,
        )

        runtime.bot.recall_message.assert_called_once_with('9001', 'group', 7)
        self.assertEqual(sent_entries, [])
        self.assertEqual(checkpointed, [])
        self.assertIn('#AB12', result)

    async def test_view_image_can_resolve_history_message_ref(self):
        runtime = self._bind_runtime_helpers(object.__new__(AIOrchestrator))
        history = [
            {
                'message_id': '321',
                'user_id': '42',
                'nickname': 'tester',
                'text': '看这个',
                'raw_message': '看这个 [CQ:image,file=a.jpg]',
                'timestamp': 1.0,
            }
        ]
        annotated, ref_map = runtime._annotate_message_refs('group', '7', history)
        ref = annotated[0]['message_ref']
        runtime.repo = SimpleNamespace(list_messages=lambda *_args, **_kwargs: history)
        runtime.vision_model = SimpleNamespace(describe_images=Mock(return_value='图像描述'))
        runtime.tools = SimpleNamespace(record_tool_use=Mock())
        runtime.config = SimpleNamespace(history_limit=20)

        result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '7',
            'agent-1',
            'view_image',
            {'message_ref': ref},
        )

        runtime.vision_model.describe_images.assert_called_once()
        self.assertEqual(runtime.vision_model.describe_images.call_args.args[0], ['a.jpg'])
        self.assertEqual(result, '图像描述')

    def test_register_persistent_message_ref_seeds_history_before_allocating(self):
        runtime = self._bind_runtime_helpers(object.__new__(AIOrchestrator))
        runtime.repo = SimpleNamespace(
            list_messages=lambda *_args, **_kwargs: [
                {
                    'message_id': '321',
                    'message_ref': 'AB12',
                    'user_id': '42',
                    'nickname': 'tester',
                    'text': '旧消息',
                    'raw_message': '旧消息',
                    'timestamp': 1.0,
                }
            ]
        )

        entry = runtime._register_persistent_message_ref(
            'group',
            '7',
            {
                'message_id': '322',
                'user_id': '42',
                'nickname': 'tester',
                'text': '新消息',
                'raw_message': '新消息',
                'timestamp': 2.0,
            },
        )

        self.assertEqual(runtime._turn_message_ref_maps['group:7']['AB12']['message_id'], '321')
        self.assertIn('message_ref', entry)
        self.assertNotEqual(entry['message_ref'], 'AB12')


if __name__ == '__main__':
    unittest.main()
