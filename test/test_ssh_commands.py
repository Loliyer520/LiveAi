import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.ai_repository import AIRepository
from core.ai_runtime import AIOrchestrator
from core.config import SSHProfileConfig
from core.dev_agent import _resolve_ssh_identity_file
from pack.json_store import JsonStore


class SSHCommandTests(unittest.IsolatedAsyncioTestCase):
    def _build_runtime(self, tmp: str):
        runtime = object.__new__(AIOrchestrator)
        runtime.repo = AIRepository(JsonStore(str(Path(tmp) / 'state.json')))
        runtime.config = SimpleNamespace(
            history_limit=20,
            admin_qq='42',
            ssh_profiles=[],
        )
        runtime.bot = SimpleNamespace(send_text=Mock())
        runtime.tools = SimpleNamespace(record_tool_use=Mock())
        runtime._ssh_profile_to_payload = AIOrchestrator._ssh_profile_to_payload
        runtime._parse_model_kv_args = AIOrchestrator._parse_model_kv_args
        runtime._parse_command_bool = AIOrchestrator._parse_command_bool
        for name in (
            '_get_stored_ssh_profiles',
            '_get_ssh_profiles_map',
            '_save_ssh_profiles',
            '_format_ssh_profiles_list',
            '_ssh_command_help_text',
            '_handle_ssh_command',
        ):
            setattr(runtime, name, getattr(AIOrchestrator, name).__get__(runtime, AIOrchestrator))
        return runtime

    async def test_ssh_add_list_update_remove_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)
            message = SimpleNamespace(chat_type='private', chat_id='42')

            await runtime._handle_ssh_command(
                message,
                '/ssh add profile_id=prod target=root@example.com root_dir=/srv/app port=2222 shell=bash strict_host_key_checking=false',
            )
            self.assertEqual(runtime.repo.get_setting('ssh_profiles')[0]['profile_id'], 'prod')
            self.assertFalse(runtime.repo.get_setting('ssh_profiles')[0]['strict_host_key_checking'])
            self.assertIn('已添加', runtime.bot.send_text.call_args_list[-1].args[2])

            await runtime._handle_ssh_command(message, '/ssh list')
            self.assertIn('prod', runtime.bot.send_text.call_args_list[-1].args[2])

            await runtime._handle_ssh_command(message, '/ssh update prod root_dir=/srv/new shell=sh strict_host_key_checking=true')
            stored = runtime.repo.get_setting('ssh_profiles')[0]
            self.assertEqual(stored['root_dir'], '/srv/new')
            self.assertEqual(stored['shell'], 'sh')
            self.assertTrue(stored['strict_host_key_checking'])
            self.assertIn('已更新', runtime.bot.send_text.call_args_list[-1].args[2])

            await runtime._handle_ssh_command(message, '/ssh remove prod')
            self.assertEqual(runtime.repo.get_setting('ssh_profiles'), [])
            self.assertIn('已删除', runtime.bot.send_text.call_args_list[-1].args[2])

    async def test_ssh_add_with_password_is_stored_but_not_listed_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)
            message = SimpleNamespace(chat_type='private', chat_id='42')

            await runtime._handle_ssh_command(
                message,
                '/ssh add profile_id=prod target=root@example.com root_dir=/srv/app password=secret123',
            )
            stored = runtime.repo.get_setting('ssh_profiles')[0]
            self.assertEqual(stored['password'], 'secret123')

            await runtime._handle_ssh_command(message, '/ssh list')
            reply = runtime.bot.send_text.call_args_list[-1].args[2]
            self.assertIn('auth:password', reply)
            self.assertNotIn('secret123', reply)

    async def test_ssh_test_command_reports_validation_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._build_runtime(tmp)
            message = SimpleNamespace(chat_type='private', chat_id='42')
            await runtime._handle_ssh_command(
                message,
                '/ssh add profile_id=prod target=root@example.com root_dir=/srv/app',
            )

            with patch('core.ai_runtime.validate_ssh_profile', return_value={
                'ok': True,
                'profile_id': 'prod',
                'target': 'root@example.com',
                'root_dir': '/srv/app',
                'remote_pwd': '/srv/app',
                'root_exists': True,
            }):
                await runtime._handle_ssh_command(message, '/ssh test prod')

            reply = runtime.bot.send_text.call_args_list[-1].args[2]
            self.assertIn('SSH 验证结果: 成功', reply)
            self.assertIn('remote_pwd: /srv/app', reply)
            self.assertIn('root_exists: True', reply)


class SSHConfigIdentityResolutionTests(unittest.TestCase):
    """~/.ssh/config 自动发现 IdentityFile：直连 IP 匹配 HostName、别名匹配 Host、
    通配段忽略、显式配置优先。"""

    def _profile(self, target: str = 'root@157.254.18.89', **overrides) -> SSHProfileConfig:
        fields = dict(
            profile_id='p',
            target=target,
            root_dir='~',
            port=22,
            identity_file='',
            password='',
            shell='bash',
            strict_host_key_checking=True,
        )
        fields.update(overrides)
        return SSHProfileConfig(**fields)

    def test_explicit_identity_file_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.ssh').mkdir()
            (Path(tmp) / '.ssh' / 'config').write_text(
                'Host mansui\n  HostName 157.254.18.89\n  IdentityFile ~/.ssh/from_config\n',
                encoding='utf-8',
            )
            profile = self._profile(identity_file='~/.ssh/explicit')
            with patch('pathlib.Path.home', return_value=Path(tmp)):
                self.assertEqual(_resolve_ssh_identity_file(profile), '~/.ssh/explicit')

    def test_resolves_identity_by_hostname_match(self):
        # profile target 是直连 IP，config 用 Host 别名 + HostName 指向该 IP
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.ssh').mkdir()
            (Path(tmp) / '.ssh' / 'config').write_text(
                'Host mansui\n  HostName 157.254.18.89\n  User root\n  IdentityFile ~/.ssh/id_ed25519_mansui\n  IdentitiesOnly yes\n',
                encoding='utf-8',
            )
            with patch('pathlib.Path.home', return_value=Path(tmp)):
                self.assertEqual(
                    _resolve_ssh_identity_file(self._profile()),
                    '~/.ssh/id_ed25519_mansui',
                )

    def test_resolves_identity_by_host_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.ssh').mkdir()
            (Path(tmp) / '.ssh' / 'config').write_text(
                'Host mansui\n  HostName 157.254.18.89\n  IdentityFile ~/.ssh/id_ed25519_mansui\n',
                encoding='utf-8',
            )
            with patch('pathlib.Path.home', return_value=Path(tmp)):
                self.assertEqual(
                    _resolve_ssh_identity_file(self._profile(target='root@mansui')),
                    '~/.ssh/id_ed25519_mansui',
                )

    def test_wildcard_host_segment_ignored(self):
        # Host * 段虽带 IdentityFile，但通配不参与匹配；具体段仍然生效
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.ssh').mkdir()
            (Path(tmp) / '.ssh' / 'config').write_text(
                'Host *\n  IdentityFile ~/.ssh/global_key\n\nHost mansui\n  HostName 157.254.18.89\n  IdentityFile ~/.ssh/id_ed25519_mansui\n',
                encoding='utf-8',
            )
            with patch('pathlib.Path.home', return_value=Path(tmp)):
                self.assertEqual(
                    _resolve_ssh_identity_file(self._profile()),
                    '~/.ssh/id_ed25519_mansui',
                )

    def test_no_match_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.ssh').mkdir()
            (Path(tmp) / '.ssh' / 'config').write_text(
                'Host other\n  HostName 10.0.0.1\n  IdentityFile ~/.ssh/other\n',
                encoding='utf-8',
            )
            with patch('pathlib.Path.home', return_value=Path(tmp)):
                self.assertEqual(_resolve_ssh_identity_file(self._profile()), '')

    def test_password_profile_skips_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.ssh').mkdir()
            (Path(tmp) / '.ssh' / 'config').write_text(
                'Host mansui\n  HostName 157.254.18.89\n  IdentityFile ~/.ssh/id_ed25519_mansui\n',
                encoding='utf-8',
            )
            profile = self._profile(password='secret')
            with patch('pathlib.Path.home', return_value=Path(tmp)):
                self.assertEqual(_resolve_ssh_identity_file(profile), '')

    def test_missing_config_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('pathlib.Path.home', return_value=Path(tmp)):
                self.assertEqual(_resolve_ssh_identity_file(self._profile()), '')


if __name__ == '__main__':
    unittest.main()
