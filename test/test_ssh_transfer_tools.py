import tempfile
import time
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import SSHProfileConfig
from core.dev_agent import (
    SSHAgentShellManager,
    DevAgentShellManager,
    _build_ssh_base_args,
    _build_tools_schema,
    _list_remote_files,
    _quote_remote_path,
    _run_ssh_command,
    _ssh_path_exists,
    _ssh_remote_path_candidates,
)


class SSHTransferToolTests(unittest.TestCase):
    def setUp(self):
        self.profile = SSHProfileConfig(
            profile_id='prod',
            target='root@example.com',
            root_dir='/srv/app',
            port=22,
            identity_file='',
            shell='bash',
            strict_host_key_checking=True,
        )

    def test_build_tools_schema_only_exposes_transfer_tools_for_ssh_agent(self):
        local_names = {tool['name'] for tool in _build_tools_schema()}
        ssh_names = {tool['name'] for tool in _build_tools_schema(ssh_enabled=True)}
        readonly_ssh_names = {tool['name'] for tool in _build_tools_schema(read_only=True, ssh_enabled=True)}

        self.assertNotIn('ssh_download_file', local_names)
        self.assertIn('ssh_download_file', ssh_names)
        self.assertIn('ssh_upload_file', ssh_names)
        self.assertIn('ssh_transfer_status', ssh_names)
        self.assertNotIn('ssh_download_file', readonly_ssh_names)
        self.assertNotIn('ssh_upload_file', readonly_ssh_names)
        self.assertIn('ssh_transfer_status', readonly_ssh_names)
        self.assertIn('ssh_transfer_cancel', readonly_ssh_names)

    def test_download_resumes_from_partial_file_and_reports_completion(self):
        content = b'hello-' + (b'x' * 512 * 1024)
        offsets = []
        reports = []

        with tempfile.TemporaryDirectory() as tmp:
            manager = SSHAgentShellManager(self.profile, project_root=tmp, on_transfer_report=reports.append)
            target = Path(tmp) / 'downloads' / 'artifact.bin'
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + '.sshpart')
            partial.write_bytes(content[:12345])

            manager._remote_file_size = lambda _path: len(content)

            def fake_read(_path, offset, size):
                offsets.append(offset)
                return content[offset:offset + size], ''

            manager._remote_read_chunk = fake_read
            result = manager.start_download('pkg/artifact.bin', 'downloads/artifact.bin', chunk_bytes=128 * 1024)
            transfer_id = result.split('transfer_id: ', 1)[1].splitlines()[0].strip()
            job = manager.transfer_jobs[transfer_id]
            job['thread'].join(timeout=3)

            self.assertEqual(job['status'], 'done')
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(offsets[0], 12345)
            self.assertTrue(any('ssh 传输完成' in item for item in reports))

    def test_upload_resumes_from_remote_partial_file(self):
        content = b'upload-' + (b'y' * 256 * 1024)
        writes = []
        reports = []

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'payload.bin'
            source.write_bytes(content)
            manager = SSHAgentShellManager(self.profile, project_root=tmp, on_transfer_report=reports.append)

            remote_resume = 8192

            def fake_remote_size(path):
                return remote_resume if str(path).endswith('.sshpart') else -1

            manager._remote_file_size = fake_remote_size
            manager._remote_write_chunk = lambda _path, offset, chunk: writes.append((offset, bytes(chunk))) or ''
            manager._remote_move_file = lambda _src, _dst: ''

            result = manager.start_upload('payload.bin', 'release/payload.bin', chunk_bytes=64 * 1024)
            transfer_id = result.split('transfer_id: ', 1)[1].splitlines()[0].strip()
            job = manager.transfer_jobs[transfer_id]
            job['thread'].join(timeout=3)

            self.assertEqual(job['status'], 'done')
            self.assertTrue(writes)
            self.assertEqual(writes[0][0], remote_resume)
            self.assertTrue(any('ssh 传输完成' in item for item in reports))

    def test_transfer_cancel_marks_job_cancelled(self):
        content = b'z' * (512 * 1024)

        with tempfile.TemporaryDirectory() as tmp:
            manager = SSHAgentShellManager(self.profile, project_root=tmp)
            manager._remote_file_size = lambda _path: len(content)

            def fake_read(_path, offset, size):
                time.sleep(0.03)
                return content[offset:offset + size], ''

            manager._remote_read_chunk = fake_read
            result = manager.start_download('logs/big.bin', 'cache/big.bin', chunk_bytes=64 * 1024)
            transfer_id = result.split('transfer_id: ', 1)[1].splitlines()[0].strip()
            time.sleep(0.01)
            cancel_result = manager.transfer_cancel(transfer_id)
            job = manager.transfer_jobs[transfer_id]
            job['thread'].join(timeout=3)

            self.assertIn('已请求取消', cancel_result)
            self.assertEqual(job['status'], 'cancelled')
            self.assertIn('cancelled', manager.transfer_status(transfer_id))

    def test_password_profile_exec_uses_unified_remote_command(self):
        profile = SSHProfileConfig(
            profile_id='prod',
            target='root@example.com',
            root_dir='/srv/app',
            port=22,
            identity_file='',
            password='secret123',
            shell='bash',
            strict_host_key_checking=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            manager = SSHAgentShellManager(profile, project_root=tmp)
            with patch('core.dev_agent._ssh_path_exists', return_value=True), patch(
                'core.dev_agent._run_ssh_command',
                return_value=(subprocess.CompletedProcess(args=['paramiko'], returncode=0, stdout=b'hello\n', stderr=b''), None),
            ) as run_remote:
                result = manager.exec('pwd', default_cwd='/')

        self.assertIn('命令执行完成', result)
        self.assertIn('hello', result)
        run_remote.assert_called_once()

    def test_key_auth_profile_exec_also_uses_unified_remote_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SSHAgentShellManager(self.profile, project_root=tmp)
            with patch('core.dev_agent._ssh_path_exists', return_value=True), patch(
                'core.dev_agent._run_ssh_command',
                return_value=(subprocess.CompletedProcess(args=['paramiko'], returncode=0, stdout=b'hello\n', stderr=b''), None),
            ) as run_remote, patch('core.dev_agent.subprocess.run') as run_ssh:
                result = manager.exec('pwd', default_cwd='/')

        self.assertIn('命令执行完成', result)
        self.assertIn('hello', result)
        run_remote.assert_called_once()
        run_ssh.assert_not_called()

    def test_quote_remote_path_expands_home_directory(self):
        self.assertEqual(_quote_remote_path('~'), '"$HOME"')
        self.assertEqual(_quote_remote_path('~/project'), '"$HOME"/project')
        self.assertEqual(_quote_remote_path('~/dir with space/app.py'), '"$HOME"/\'dir with space\'/app.py')

    def test_ssh_shell_default_timeout_is_longer_than_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            ssh_manager = SSHAgentShellManager(self.profile, project_root=tmp)
            local_manager = DevAgentShellManager(tmp)

        self.assertEqual(ssh_manager._normalize_timeout(None, background=False), 60)
        self.assertEqual(local_manager._normalize_timeout(None, background=False), 20)

    def test_build_ssh_base_args_disables_interactive_retries(self):
        with patch('core.dev_agent._ensure_ssh_binary', return_value='ssh'):
            args = _build_ssh_base_args(self.profile)

        self.assertIn('BatchMode=yes', args)
        self.assertIn('ConnectTimeout=10', args)
        self.assertIn('ConnectionAttempts=1', args)

    def test_ssh_remote_path_candidates_keep_root_relative_then_absolute_fallback(self):
        profile = SSHProfileConfig(
            profile_id='prod',
            target='root@example.com',
            root_dir='~',
            port=22,
            identity_file='',
            shell='bash',
            strict_host_key_checking=True,
        )
        self.assertEqual(_ssh_remote_path_candidates(profile, '/my/pro/'), ['~/my/pro', '/my/pro'])

    def test_resolve_cwd_falls_back_to_absolute_remote_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SSHAgentShellManager(self.profile, project_root=tmp)
            with patch('core.dev_agent._ssh_path_exists', side_effect=[False, True, True]):
                remote_cwd, display_cwd = manager._resolve_cwd('/my/pro/', default_cwd='/')

        self.assertEqual(display_cwd, '/my/pro')
        self.assertEqual(remote_cwd, '/my/pro')

    def test_ssh_path_exists_retries_once_after_timeout(self):
        with patch(
            'core.dev_agent._run_ssh_command',
            side_effect=[
                (None, 'SSH 命令执行超时（45 秒）。'),
                (subprocess.CompletedProcess(args=['ssh'], returncode=0, stdout=b'', stderr=b''), None),
            ],
        ) as run_remote:
            exists = _ssh_path_exists(self.profile, '/my/pro', 'dir')

        self.assertTrue(exists)
        self.assertEqual(run_remote.call_count, 2)

    def test_list_remote_files_retries_once_after_timeout(self):
        with patch('core.dev_agent._ssh_resolve_existing_path', return_value=('/my/pro', ['~/my/pro', '/my/pro'])), patch(
            'core.dev_agent._run_ssh_command',
            side_effect=[
                (None, 'SSH 命令执行超时（45 秒）。'),
                (subprocess.CompletedProcess(args=['ssh'], returncode=0, stdout=b'api/\n', stderr=b''), None),
            ],
        ) as run_remote:
            result = _list_remote_files(self.profile, '/my/pro/')

        self.assertEqual(result, 'api/')
        self.assertEqual(run_remote.call_count, 2)

    def test_run_ssh_command_prefers_paramiko_for_key_auth(self):
        with patch('core.dev_agent._load_paramiko_module', return_value=object()), patch(
            'core.dev_agent._run_paramiko_command',
            return_value=(subprocess.CompletedProcess(args=['paramiko'], returncode=0, stdout=b'ok\n', stderr=b''), None),
        ) as run_paramiko, patch(
            'core.dev_agent.subprocess.run',
        ) as run_ssh:
            completed, err = _run_ssh_command(self.profile, 'test -d /my/pro', timeout_seconds=45)

        self.assertIsNone(err)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.returncode, 0)
        run_paramiko.assert_called_once()
        run_ssh.assert_not_called()

    def test_run_ssh_command_falls_back_to_system_ssh_after_paramiko_failure(self):
        with patch('core.dev_agent._load_paramiko_module', return_value=object()), patch(
            'core.dev_agent._run_paramiko_command',
            return_value=(None, 'Paramiko 执行失败: auth failed'),
        ) as run_paramiko, patch('core.dev_agent._ensure_ssh_binary', return_value='ssh'), patch(
            'core.dev_agent.subprocess.run',
            return_value=subprocess.CompletedProcess(args=['ssh'], returncode=0, stdout=b'ok\n', stderr=b''),
        ) as run_ssh:
            completed, err = _run_ssh_command(self.profile, 'test -d /my/pro', timeout_seconds=45)

        self.assertIsNone(err)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.returncode, 0)
        run_paramiko.assert_called_once()
        run_ssh.assert_called_once()


if __name__ == '__main__':
    unittest.main()
