import unittest
from types import SimpleNamespace

from core.agent_manager import AgentManager


class AgentProgressReportsTests(unittest.TestCase):
    def test_build_tool_progress_report_summarizes_commands_and_changed_files(self):
        report = AgentManager._build_tool_progress_report([
            SimpleNamespace(name='shell_exec', input={'command': 'git status'}),
            SimpleNamespace(name='edit_local_file', input={'path': 'core/ai_runtime.py'}),
            SimpleNamespace(name='github_delete_file', input={'path': 'old.txt'}),
        ])

        self.assertIn('执行命令: `git status`', report)
        self.assertIn('修改文件: `core/ai_runtime.py`', report)
        self.assertIn('删除文件: `old.txt`', report)

    def test_emit_progress_report_enqueues_progress_metadata(self):
        manager = AgentManager()
        agent_id = manager.create_agent('do work', origin_scope='private:7')
        report = manager._build_tool_progress_report([
            SimpleNamespace(name='shell_exec', input={'command': 'git status'}),
            SimpleNamespace(name='edit_local_file', input={'path': 'core/ai_runtime.py'}),
        ])

        manager.emit_progress_report(agent_id, report)
        reports = manager.drain_pending_reports()

        progress_reports = [item for item in reports if item.get('report_type') == 'progress']
        self.assertEqual(len(progress_reports), 1)
        self.assertIn('执行命令: `git status`', progress_reports[0]['text'])
        self.assertIn('修改文件: `core/ai_runtime.py`', progress_reports[0]['text'])


if __name__ == '__main__':
    unittest.main()
