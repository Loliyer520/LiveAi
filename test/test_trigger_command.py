import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.ai_repository import AIRepository
from core.ai_runtime import AIOrchestrator
from pack.json_store import JsonStore


class TriggerCommandTests(unittest.IsolatedAsyncioTestCase):
    def _build_runtime(self, tmp: str):
        runtime = object.__new__(AIOrchestrator)
        runtime.repo = AIRepository(
            JsonStore(str(Path(tmp) / 'state.json')),
            default_trigger_rate=0.08,
        )
        runtime.config = SimpleNamespace(
            history_limit=20,
            admin_qq='42',
            global_trigger_rate=0.08,
        )
        runtime.bot = SimpleNamespace(send_text=Mock())
        runtime.loop = None
        runtime.tools = SimpleNamespace(record_tool_use=Mock())
        for name in (
            '_is_admin_message',
            '_send_chat_reply',
            '_handle_trigger_command',
            '_apply_global_trigger_rate',
            '_run_ai_tool_call',
            '_short_text',
        ):
            setattr(runtime, name, getattr(AIOrchestrator, name).__get__(runtime, AIOrchestrator))
        return runtime

    async def test_trigger_command_reads_current_global_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)
            message = SimpleNamespace(user_id='42', chat_type='private', chat_id='42')

            await runtime._handle_trigger_command(message, '/trigger')

            reply = runtime.bot.send_text.call_args.args[2]
            self.assertIn('当前全局随机触发概率：0.080', reply)
            self.assertIn('/trigger 0.08', reply)

    async def test_trigger_command_updates_existing_and_future_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)
            message = SimpleNamespace(user_id='42', chat_type='private', chat_id='42')
            runtime.repo.get_or_create_agent('group', '100')
            runtime.repo.get_or_create_master()

            with patch('core.ai_runtime.save_config_to_yaml', return_value=True) as save_mock:
                await runtime._handle_trigger_command(message, '/trigger 0.12')

            save_mock.assert_called_once_with({'ai': {'global_trigger_rate': 0.12}})
            self.assertEqual(runtime.config.global_trigger_rate, 0.12)
            self.assertEqual(runtime.repo.default_trigger_rate, 0.12)
            self.assertAlmostEqual(runtime.repo.get_or_create_agent('group', '100').trigger_rate, 0.12)
            self.assertAlmostEqual(runtime.repo.get_or_create_agent('group', '101').trigger_rate, 0.12)
            self.assertAlmostEqual(runtime.repo.get_or_create_master().trigger_rate, 0.08)

            reply = runtime.bot.send_text.call_args.args[2]
            self.assertIn('全局随机触发概率已设为：0.120', reply)
            self.assertIn('已同步现有会话：1 个', reply)

    async def _call_tool(self, runtime, tool_input: dict) -> str:
        return await runtime._run_ai_tool_call(
            'master', 'global', 'master:global', 'set_trigger_rate', tool_input
        )

    async def test_tool_global_target_persists_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)
            runtime.repo.get_or_create_agent('group', '100')

            with patch('core.ai_runtime.save_config_to_yaml', return_value=True) as save_mock:
                result = await self._call_tool(
                    runtime, {'rate': 0, 'target_scope_type': 'global'}
                )

            save_mock.assert_called_once_with({'ai': {'global_trigger_rate': 0.0}})
            self.assertEqual(runtime.config.global_trigger_rate, 0.0)
            self.assertEqual(runtime.repo.default_trigger_rate, 0.0)
            self.assertAlmostEqual(runtime.repo.get_or_create_agent('group', '100').trigger_rate, 0.0)
            # 新会话也要继承，这才是重启后不回弹的关键
            self.assertAlmostEqual(runtime.repo.get_or_create_agent('group', '999').trigger_rate, 0.0)
            self.assertIn('全局默认随机触发概率已设为：0.000', result)

    async def test_tool_global_target_reports_persist_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)

            with patch('core.ai_runtime.save_config_to_yaml', return_value=False):
                result = await self._call_tool(
                    runtime, {'rate': 0, 'target_scope_type': 'global'}
                )

            self.assertIn('写入 config.yaml 失败', result)

    async def test_tool_global_target_without_rate_reads_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)

            result = await self._call_tool(runtime, {'target_scope_type': 'global'})

            self.assertIn('全局默认随机触发概率：0.080', result)

    async def test_tool_single_scope_does_not_touch_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)

            with patch('core.ai_runtime.save_config_to_yaml') as save_mock:
                result = await self._call_tool(
                    runtime,
                    {'rate': 0, 'target_scope_type': 'group', 'target_scope_id': '100'},
                )

            save_mock.assert_not_called()
            self.assertEqual(runtime.config.global_trigger_rate, 0.08)
            self.assertAlmostEqual(runtime.repo.get_or_create_agent('group', '100').trigger_rate, 0.0)
            self.assertAlmostEqual(runtime.repo.get_or_create_agent('group', '101').trigger_rate, 0.08)
            self.assertIn('target_scope_type="global"', result)

    async def test_tool_global_target_rejected_for_child_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)

            with patch('core.ai_runtime.save_config_to_yaml') as save_mock:
                result = await runtime._run_ai_tool_call(
                    'group', '100', 'group:100', 'set_trigger_rate',
                    {'rate': 0.2, 'target_scope_type': 'global'},
                )

            save_mock.assert_not_called()
            self.assertEqual(runtime.config.global_trigger_rate, 0.08)
            self.assertIn('仅主 AI', result)


if __name__ == '__main__':
    unittest.main()
