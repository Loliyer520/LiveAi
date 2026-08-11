"""回归：Anthropic extended thinking 的 assistant 消息必须原样回传。

历史上 `_filter_thinking_blocks` 被用于“回传给 API 的下一轮 model_messages”，
把 thinking block 剥掉后触发上游 400：
    The `content[].thinking` in the thinking mode must be passed back to the API.

本测试锁定：`_complete_child_turn` 工具循环内，assistant 消息的 content
必须原样保留 thinking block（含 signature），不再做 thinking 过滤。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.ai_runtime import AIOrchestrator


def _thinking_reply(*, text='', tool_calls=(), thinking_text='让我想想', signature='sig-abc'):
    blocks = [
        {'type': 'thinking', 'thinking': thinking_text, 'signature': signature},
    ]
    for name, tool_input, call_id in tool_calls:
        blocks.append({'type': 'tool_use', 'id': call_id, 'name': name, 'input': tool_input})
    if text or not tool_calls:
        blocks.append({'type': 'text', 'text': text})
    return SimpleNamespace(
        text=text,
        tool_calls=[
            SimpleNamespace(name=name, input=tool_input, call_id=call_id)
            for name, tool_input, call_id in tool_calls
        ],
        raw_content=blocks,
        stop_reason='tool_use' if tool_calls else 'end_turn',
    )


class ThinkingBlockReplayTests(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(admin_qq=1)
        runtime.model = SimpleNamespace(is_openai_protocol=False)
        runtime.tools = Mock()
        runtime._scope_thinking_levels = {}
        runtime._scope_session_modes = {}
        runtime._scope_key = lambda scope_type, scope_id: f'{scope_type}:{scope_id}'
        runtime._is_epoch_stale = lambda _epoch: False
        runtime._consume_send_message_persona_notice = Mock(return_value=False)
        runtime._run_ai_tool_call = AsyncMock(return_value='ok')
        runtime._apply_directive_tools = Mock(return_value='')
        runtime._normalize_think_note = lambda text: text or ''
        runtime._record_turn_log = AsyncMock()
        runtime._complete_chat = AsyncMock()
        return runtime

    async def test_tool_loop_replays_assistant_thinking_block_verbatim(self):
        runtime = self._make_runtime()
        # 第一轮：带 thinking block 的 assistant 消息 + LOOP 工具（memory_list）
        first = _thinking_reply(
            tool_calls=[('memory_list', {'keyword': 'x'}, 'call-1')],
            thinking_text='第一轮思考',
            signature='sig-1',
        )
        # 第二轮：带 thinking block 的最终文本回复
        second = _thinking_reply(text='查询完成', thinking_text='第二轮思考', signature='sig-2')
        runtime._complete_chat.side_effect = [first, second]

        bundle, _ms, used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [{'type': 'text', 'text': 'sys'}], 'messages': [{'role': 'user', 'content': '查一下'}]},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
        )

        self.assertTrue(used_tools)
        self.assertEqual(bundle['message'], '')
        # 第二次请求的 messages 里，assistant 消息必须原样保留 thinking block
        calls = runtime._complete_chat.await_args_list
        self.assertEqual(len(calls), 2)
        second_request_messages = calls[1].args[1]
        assistant_msgs = [m for m in second_request_messages if m.get('role') == 'assistant']
        self.assertEqual(len(assistant_msgs), 1)
        content = assistant_msgs[0]['content']
        self.assertIsInstance(content, list)
        thinking_blocks = [b for b in content if b.get('type') == 'thinking']
        self.assertEqual(len(thinking_blocks), 1)
        self.assertEqual(thinking_blocks[0]['thinking'], '第一轮思考')
        self.assertEqual(thinking_blocks[0]['signature'], 'sig-1')
        # tool_use block 也必须原样保留
        tool_blocks = [b for b in content if b.get('type') == 'tool_use']
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]['id'], 'call-1')


if __name__ == '__main__':
    unittest.main()
