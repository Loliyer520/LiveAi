import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_runtime import AIOrchestrator
from core.ai_types import AgentProfile
from core.ai_tools_schema import build_tools


class ThinkingLevelToolTests(unittest.IsolatedAsyncioTestCase):
    def test_build_tools_exposes_set_thinking_level(self):
        names = {item['name'] for item in build_tools()}
        self.assertIn('set_thinking_level', names)
        self.assertIn('set_trigger_rate', names)

    async def test_set_thinking_level_updates_current_scope(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_thinking_levels = {}
        runtime._scope_key = AIOrchestrator._scope_key.__get__(runtime, AIOrchestrator)
        runtime._get_scope_thinking_level = AIOrchestrator._get_scope_thinking_level.__get__(runtime, AIOrchestrator)
        runtime._format_ts_text = lambda *_args, **_kwargs: ''
        runtime._short_text = lambda text, _limit=0: str(text or '')
        runtime.config = SimpleNamespace(history_limit=20)
        runtime.tools = SimpleNamespace(
            record_tool_use=Mock(),
        )

        get_result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '321',
            'agent:group:321',
            'set_thinking_level',
            {},
        )
        self.assertIn('当前会话思考等级：low', get_result)

        set_result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '321',
            'agent:group:321',
            'set_thinking_level',
            {'level': 'high'},
        )
        self.assertEqual(runtime._scope_thinking_levels['group:321'], 'high')
        self.assertIn('已设为：high', set_result)
        self.assertIn('重启恢复 low', set_result)

        clear_result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '321',
            'agent:group:321',
            'set_thinking_level',
            {'level': 'off'},
        )
        self.assertEqual(runtime._scope_thinking_levels['group:321'], 'off')
        self.assertIn('已设为：off', clear_result)

    async def test_set_trigger_rate_updates_current_scope(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_thinking_levels = {}
        runtime._scope_key = AIOrchestrator._scope_key.__get__(runtime, AIOrchestrator)
        runtime._get_scope_thinking_level = AIOrchestrator._get_scope_thinking_level.__get__(runtime, AIOrchestrator)
        runtime._format_ts_text = lambda *_args, **_kwargs: ''
        runtime._short_text = lambda text, _limit=0: str(text or '')
        runtime.config = SimpleNamespace(history_limit=20)
        initial_agent = AgentProfile(agent_id='agent:group:321', scope_type='group', scope_id='321', role='child')
        updated_agent = AgentProfile(
            agent_id='agent:group:321',
            scope_type='group',
            scope_id='321',
            role='child',
            trigger_rate=0.2,
        )
        runtime.repo = SimpleNamespace(
            get_or_create_agent=Mock(return_value=initial_agent),
            update_agent_trigger_rate=Mock(return_value=updated_agent),
        )
        runtime.tools = SimpleNamespace(
            record_tool_use=Mock(),
        )

        get_result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '321',
            'agent:group:321',
            'set_trigger_rate',
            {},
        )
        self.assertIn('当前会话随机触发概率：0.000', get_result)

        set_result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '321',
            'agent:group:321',
            'set_trigger_rate',
            {'rate': 0.2},
        )
        runtime.repo.update_agent_trigger_rate.assert_called_once_with('group', '321', 0.2)
        self.assertIn('已设为：0.200', set_result)

        invalid_result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '321',
            'agent:group:321',
            'set_trigger_rate',
            {'rate': 0.5},
        )
        self.assertIn('超出范围', invalid_result)

    async def test_set_trigger_rate_master_can_target_other_scope(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_thinking_levels = {}
        runtime._scope_key = AIOrchestrator._scope_key.__get__(runtime, AIOrchestrator)
        runtime._get_scope_thinking_level = AIOrchestrator._get_scope_thinking_level.__get__(runtime, AIOrchestrator)
        runtime._format_ts_text = lambda *_args, **_kwargs: ''
        runtime._short_text = lambda text, _limit=0: str(text or '')
        runtime.config = SimpleNamespace(history_limit=20)
        updated_agent = AgentProfile(
            agent_id='agent:group:888',
            scope_type='group',
            scope_id='888',
            role='child',
            trigger_rate=0.05,
        )
        runtime.repo = SimpleNamespace(
            get_or_create_agent=Mock(return_value=updated_agent),
            update_agent_trigger_rate=Mock(return_value=updated_agent),
        )
        runtime.tools = SimpleNamespace(record_tool_use=Mock())

        result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'master',
            '0',
            'agent:master:0',
            'set_trigger_rate',
            {'rate': 0.05, 'target_scope_type': 'group', 'target_scope_id': '888'},
        )
        runtime.repo.update_agent_trigger_rate.assert_called_once_with('group', '888', 0.05)
        self.assertIn('已设为：0.050', result)

    async def test_set_trigger_rate_child_cannot_target_other_scope(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_thinking_levels = {}
        runtime._scope_key = AIOrchestrator._scope_key.__get__(runtime, AIOrchestrator)
        runtime._get_scope_thinking_level = AIOrchestrator._get_scope_thinking_level.__get__(runtime, AIOrchestrator)
        runtime._format_ts_text = lambda *_args, **_kwargs: ''
        runtime._short_text = lambda text, _limit=0: str(text or '')
        runtime.config = SimpleNamespace(history_limit=20)
        runtime.repo = SimpleNamespace(
            get_or_create_agent=Mock(),
            update_agent_trigger_rate=Mock(),
        )
        runtime.tools = SimpleNamespace(record_tool_use=Mock())

        result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '321',
            'agent:group:321',
            'set_trigger_rate',
            {'rate': 0.05, 'target_scope_type': 'group', 'target_scope_id': '888'},
        )
        runtime.repo.update_agent_trigger_rate.assert_not_called()
        self.assertIn('仅主 AI', result)


if __name__ == '__main__':
    unittest.main()
