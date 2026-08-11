import os
import shutil
import subprocess
import sys
import unittest

from core.dev_agent import DevAgentShellManager, _bash_exec_argv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ShellExecVariableTests(unittest.TestCase):
    """回归：本地 shell_exec 必须保留命令中的 $变量语义。

    背景：Windows 上 `System32/bash.exe` 是 WSL launcher shim，经 `wsl.exe`
    默认模式二次解释命令，会破坏引号内的 $变量（export 后立即读取为空）。
    修复方式是检测到该 shim 时改用 `wsl --exec bash` 直接 exec。
    """

    @unittest.skipUnless(sys.platform == 'win32', '仅在 Windows 上验证 WSL shim 检测')
    def test_bash_exec_argv_prefers_wsl_exec_on_windows_shim(self):
        system_root = os.environ.get('SystemRoot') or r'C:\Windows'
        shim_path = os.path.join(system_root, 'System32', 'bash.exe')
        if os.path.exists(shim_path):
            self.assertEqual(['wsl', '--exec', 'bash'], _bash_exec_argv())
        else:
            self.assertEqual(['bash'], _bash_exec_argv())

    @unittest.skipUnless(shutil.which('bash'), '本机没有 bash，跳过真实执行用例')
    def test_exec_preserves_exported_variable(self):
        manager = DevAgentShellManager(PROJECT_ROOT)
        result = manager.exec('export FOO_TEST=abc; echo "AFTER=$FOO_TEST"')
        self.assertIn('AFTER=abc', result)

    @unittest.skipUnless(shutil.which('bash'), '本机没有 bash，跳过真实执行用例')
    def test_exec_preserves_variable_in_subshell_and_pipeline(self):
        manager = DevAgentShellManager(PROJECT_ROOT)
        result = manager.exec(
            'FOO_TEST=abc; echo "$FOO_TEST" | tr a-z A-Z; echo "SUB=$(echo $FOO_TEST)"'
        )
        self.assertIn('ABC', result)
        self.assertIn('SUB=abc', result)

    @unittest.skipUnless(shutil.which('bash'), '本机没有 bash，跳过真实执行用例')
    def test_exec_works_with_single_quotes_inside_command(self):
        manager = DevAgentShellManager(PROJECT_ROOT)
        result = manager.exec("echo 'hello world'; export FOO_TEST=abc; echo \"AFTER=$FOO_TEST\"")
        self.assertIn('hello world', result)
        self.assertIn('AFTER=abc', result)


if __name__ == '__main__':
    unittest.main()
