import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.ai_runtime import AIOrchestrator
from core.ai_tools_schema import build_tools


def _names(tools):
    return {tool['name'] for tool in (tools or [])}


class ChatModeToolGapTests(unittest.TestCase):
    """chat 模式关掉 allow_tasks，agent 相关工具整组消失 —— "拉不到 agent 列表"的来源。"""

    def test_agent_tools_are_absent_in_chat_mode(self):
        chat = _names(build_tools(chat_mode=True, allow_tasks=True))
        for name in ('create_agent', 'list_agents', 'peek_agent', 'send_to_agent', 'destroy_agent'):
            self.assertNotIn(name, chat)

    def test_agent_tools_return_in_code_mode(self):
        code = _names(build_tools(chat_mode=False, allow_tasks=True))
        for name in ('create_agent', 'list_agents', 'peek_agent', 'send_to_agent', 'destroy_agent'):
            self.assertIn(name, code)

    def test_set_session_mode_is_reachable_from_chat_mode(self):
        # 否则会话被锁死在 chat，子 AI 连切回来的手段都没有
        self.assertIn('set_session_mode', _names(build_tools(chat_mode=True)))

    def test_cache_marker_is_single_per_table(self):
        for tools in (build_tools(chat_mode=True), build_tools(chat_mode=False)):
            self.assertIn('cache_control', tools[-1])
            self.assertEqual(1, sum(1 for tool in tools if 'cache_control' in tool))


class MidTurnToolRebuildTests(unittest.IsolatedAsyncioTestCase):
    """同一轮里切到 code 后，剩下的迭代必须拿到新工具表，否则模型"切了还是没工具"。"""

    def _runtime(self, mode=None):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(admin_qq=1, history_limit=20)
        runtime.model = SimpleNamespace(is_openai_protocol=False)
        runtime.tools = Mock()
        runtime.tools.record_tool_use = Mock()
        runtime._scope_thinking_levels = {}
        runtime._scope_session_modes = {'group:7': mode} if mode else {}
        runtime._scope_code_idle_turns = {}
        runtime._pending_self_interrupts = {}
        runtime._pending_send_message_persona_notices = {}
        runtime._character_sessions = SimpleNamespace(pop_tool_raw=lambda _scope_key: None)
        runtime._scope_key = lambda scope_type, scope_id: f'{scope_type}:{scope_id}'
        runtime._is_epoch_stale = lambda _epoch: False
        runtime._filter_thinking_blocks = lambda raw: raw
        runtime._normalize_think_note = lambda text: text or ''
        runtime._record_turn_log = AsyncMock()
        return runtime

    async def _run_turn(self, runtime, switch_to=None):
        """第一轮调 set_session_mode，第二轮结束。返回每轮实际拿到的工具名集合。"""
        seen_tools = []

        async def fake_complete_chat(_system_blocks, _messages, round_tools, _temperature, scope_key=None, role=None):
            seen_tools.append(_names(round_tools))
            if len(seen_tools) == 1:
                return SimpleNamespace(
                    text='',
                    tool_calls=[SimpleNamespace(name='set_session_mode', input={'mode': switch_to or 'code'}, call_id='c1')],
                    raw_content='tool use',
                    stop_reason='tool_use',
                )
            return SimpleNamespace(text='好', tool_calls=[], raw_content='好', stop_reason='end_turn')

        async def fake_run_tool(*_args, **_kwargs):
            # 真实处理器就是这么改的；这里只关心改完之后工具表跟不跟。
            if switch_to:
                runtime._set_scope_session_mode('group', '7', switch_to)
            return '模式已设为：' + (switch_to or 'code')

        runtime._complete_chat = fake_complete_chat
        runtime._run_ai_tool_call = fake_run_tool

        await runtime._complete_child_turn(
            'group',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=None,
        )
        return seen_tools

    async def test_agent_tools_arrive_in_the_same_turn_after_switching(self):
        runtime = self._runtime('chat')
        seen = await self._run_turn(runtime, switch_to='code')

        self.assertEqual(2, len(seen))
        self.assertNotIn('list_agents', seen[0])
        self.assertIn('list_agents', seen[1], '切到 code 后本轮就该能拉 agent 列表')
        self.assertIn('create_agent', seen[1])

    async def test_table_is_stable_when_mode_does_not_change(self):
        runtime = self._runtime('chat')
        seen = await self._run_turn(runtime, switch_to='chat')

        self.assertEqual(2, len(seen))
        self.assertEqual(seen[0], seen[1], '模式没变就不该换表，白打掉工具表的 prompt cache')
        self.assertNotIn('list_agents', seen[0])

    async def test_switching_back_to_chat_drops_the_work_tools(self):
        runtime = self._runtime('code')
        seen = await self._run_turn(runtime, switch_to='chat')

        self.assertIn('list_agents', seen[0])
        self.assertNotIn('list_agents', seen[1])

    async def test_mode_switch_does_not_leak_to_other_scopes(self):
        runtime = self._runtime('chat')
        await self._run_turn(runtime, switch_to='code')

        self.assertEqual('code', runtime._get_scope_session_mode('group', '7'))
        self.assertEqual('chat', runtime._get_scope_session_mode('group', '8'))

    async def test_first_round_reflects_the_mode_at_turn_start(self):
        runtime = self._runtime('code')
        seen = await self._run_turn(runtime, switch_to='code')
        self.assertIn('list_agents', seen[0])
        self.assertEqual(seen[0], seen[1])


if __name__ == '__main__':
    unittest.main()
