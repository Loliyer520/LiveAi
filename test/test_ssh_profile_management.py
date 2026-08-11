import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.ai_repository import AIRepository
from core.ai_runtime import AIOrchestrator
from core.ai_tools_schema import build_tools
from core.config import SSHProfileConfig
from pack.json_store import JsonStore


class SSHProfileManagementTests(unittest.IsolatedAsyncioTestCase):
    def _build_runtime(self, tmp: str, config_profiles=None):
        runtime = object.__new__(AIOrchestrator)
        runtime.repo = AIRepository(JsonStore(str(Path(tmp) / 'state.json')))
        runtime.config = SimpleNamespace(
            history_limit=20,
            admin_qq='42',
            ssh_profiles=list(config_profiles or []),
        )
        runtime.tools = SimpleNamespace(record_tool_use=Mock())
        runtime._ssh_profile_to_payload = AIOrchestrator._ssh_profile_to_payload
        for name in (
            '_is_admin_user',
            '_get_stored_ssh_profiles',
            '_get_ssh_profiles_map',
            '_save_ssh_profiles',
            '_format_ssh_profiles_list',
        ):
            setattr(runtime, name, getattr(AIOrchestrator, name).__get__(runtime, AIOrchestrator))
        return runtime

    def test_build_tools_exposes_ssh_management_tools(self):
        names = {item['name'] for item in build_tools(allow_config_tools=True, allow_tasks=True)}
        self.assertIn('manage_ssh_profile', names)
        self.assertIn('validate_ssh_profile', names)
        self.assertIn('create_ssh_agent', names)
        self.assertIn('list_ssh_profiles', names)

    async def test_manage_ssh_profile_add_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)
            add_result = await AIOrchestrator._run_ai_tool_call(
                runtime,
                'private',
                '42',
                'agent:test',
                'manage_ssh_profile',
                {
                    'action': 'add',
                    'profile_id': 'prod',
                    'target': 'root@example.com',
                    'root_dir': '/srv/app',
                    'port': 2222,
                    'shell': 'bash',
                },
            )

            self.assertIn('已添加', add_result)
            stored = runtime.repo.get_setting('ssh_profiles', [])
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]['profile_id'], 'prod')
            self.assertEqual(stored[0]['port'], 2222)

            with patch('core.ai_runtime.validate_ssh_profile', return_value={'ok': True, 'profile_id': 'prod'}):
                validate_result = await AIOrchestrator._run_ai_tool_call(
                    runtime,
                    'private',
                    '42',
                    'agent:test',
                    'validate_ssh_profile',
                    {'profile_id': 'prod'},
                )

        payload = json.loads(validate_result)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['profile_id'], 'prod')

    async def test_manage_ssh_profile_remove_can_clear_config_fallback(self):
        config_profile = SSHProfileConfig(
            profile_id='default',
            target='root@default.example.com',
            root_dir='/srv/default',
            port=22,
            identity_file='',
            shell='bash',
            strict_host_key_checking=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp, config_profiles=[config_profile])
            before = runtime._format_ssh_profiles_list()
            remove_result = await AIOrchestrator._run_ai_tool_call(
                runtime,
                'private',
                '42',
                'agent:test',
                'manage_ssh_profile',
                {
                    'action': 'remove',
                    'profile_id': 'default',
                },
            )
            after = runtime._format_ssh_profiles_list()

        self.assertIn('default', before)
        self.assertIn('已删除', remove_result)
        self.assertEqual(after, '当前未配置任何 SSH profile。')


if __name__ == '__main__':
    unittest.main()
