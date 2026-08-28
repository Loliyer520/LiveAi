import os
import tempfile
import unittest
from pathlib import Path

from core.agent_manager import AgentManager
from core.dev_agent import (
    DevAgentShellManager,
    _normalize_agent_cwd_spec,
    _read_local_file,
    _resolve_agent_workspace_root,
)
from pack.json_store import JsonStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LocalAgentWorkspaceTests(unittest.TestCase):
    def test_legacy_relative_cwd_is_preserved(self):
        self.assertEqual(_normalize_agent_cwd_spec('/core'), '~/core')
        self.assertEqual(_normalize_agent_cwd_spec('~/core'), '~/core')
        self.assertIsNone(_normalize_agent_cwd_spec('../outside'))

    def test_workspace_root_validation_rejects_missing_and_broad_roots(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(_resolve_agent_workspace_root(root), os.path.realpath(root))
        self.assertIsNone(_resolve_agent_workspace_root('/does/not/exist'))
        self.assertIsNone(_resolve_agent_workspace_root('/'))
        self.assertIsNone(_resolve_agent_workspace_root('/mnt/c'))
        self.assertIsNone(_resolve_agent_workspace_root('/proc'))
        if os.path.exists('/tmp'):
            self.assertIsNone(_resolve_agent_workspace_root('/tmp'))

    def test_workspace_root_persists_and_legacy_defaults_to_project_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
            manager = AgentManager(JsonStore(str(Path(state) / 'agents.json')))
            agent_id = manager.create_agent('test', workspace_root=root)
            self.assertEqual(manager.get_agent(agent_id)['workspace_root'], os.path.realpath(root))
            legacy_id = manager.create_agent('legacy')
            self.assertNotIn('workspace_root', manager.get_agent(legacy_id))

    @unittest.skipUnless(os.name == 'posix' and os.path.exists('/bin/bash'), 'requires Unix bash')
    def test_shell_and_file_tools_share_workspace_root_and_block_escape(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / 'workspace'
            root.mkdir()
            (root / 'inside.txt').write_text('inside', encoding='utf-8')
            (Path(parent) / 'outside.txt').write_text('outside', encoding='utf-8')
            (root / 'escape').symlink_to(Path(parent), target_is_directory=True)
            nested = root / 'nested'
            nested.mkdir()

            self.assertIn('inside', _read_local_file(str(root), 'inside.txt'))
            self.assertIn('路径不合法', _read_local_file(str(root), '../outside.txt'))
            self.assertIn('路径不合法', _read_local_file(str(root), 'nested/../../outside.txt'))
            self.assertIn('路径不合法', _read_local_file(str(root), 'escape/outside.txt'))

            manager = DevAgentShellManager(str(root))
            try:
                shell_output = manager.exec('pwd; ls inside.txt')
                self.assertIn(str(root), shell_output)
                self.assertIn('inside.txt', shell_output)
                self.assertIn('工作目录不合法', manager.exec('pwd', cwd='../'))
            finally:
                manager.shutdown()


if __name__ == '__main__':
    unittest.main()
