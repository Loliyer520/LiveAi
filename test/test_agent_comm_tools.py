import unittest

from core.agent_manager import AgentManager
from core.dev_agent import (
    RESIDENT_AGENT_COMM_TOOL_NAMES,
    _build_capability_matrix,
    _build_tools_schema,
)


class ResidentCommToolSchemaTests(unittest.TestCase):
    def test_comm_tools_absent_for_one_shot_task(self):
        names = {tool['name'] for tool in _build_tools_schema()}
        self.assertFalse(names & RESIDENT_AGENT_COMM_TOOL_NAMES)

    def test_comm_tools_present_for_resident_agent(self):
        names = {tool['name'] for tool in _build_tools_schema(resident=True)}
        self.assertEqual(
            {'report_progress', 'ask_supervisor', 'finish_task'},
            names & RESIDENT_AGENT_COMM_TOOL_NAMES,
        )

    def test_comm_tools_survive_read_only_filter(self):
        # 只读 agent 同样要能汇报/提问/完成，出口不该被只读白名单过滤掉
        names = {tool['name'] for tool in _build_tools_schema(read_only=True, resident=True)}
        self.assertTrue(RESIDENT_AGENT_COMM_TOOL_NAMES <= names)

    def test_required_fields_declared(self):
        schema = {tool['name']: tool['input_schema'] for tool in _build_tools_schema(resident=True)}
        self.assertEqual(['text'], schema['report_progress']['required'])
        self.assertEqual(['question'], schema['ask_supervisor']['required'])
        self.assertEqual(['summary'], schema['finish_task']['required'])

    def test_capability_matrix_mentions_comm_tools_only_when_resident(self):
        self.assertNotIn('ask_supervisor', _build_capability_matrix())
        resident = _build_capability_matrix(resident=True)
        for name in RESIDENT_AGENT_COMM_TOOL_NAMES:
            self.assertIn(name, resident)


class CommToolFormattingTests(unittest.TestCase):
    def test_progress_text_is_labelled(self):
        text = AgentManager._format_comm_tool_text('report_progress', {'text': '读完了 runtime'})
        self.assertIn('进展', text)
        self.assertIn('读完了 runtime', text)

    def test_question_includes_options_and_recommendation(self):
        text = AgentManager._format_comm_tool_text('ask_supervisor', {
            'question': '改哪个文件',
            'options': ['改 A', '改 B', '  '],
            'recommendation': '倾向 A',
        })
        self.assertIn('改哪个文件', text)
        self.assertIn('1. 改 A', text)
        self.assertIn('2. 改 B', text)
        self.assertNotIn('3.', text)
        self.assertIn('倾向 A', text)

    def test_finish_includes_follow_up(self):
        text = AgentManager._format_comm_tool_text('finish_task', {
            'summary': '改完并跑过测试',
            'follow_up': '建议顺手补文档',
        })
        self.assertIn('改完并跑过测试', text)
        self.assertIn('建议顺手补文档', text)

    def test_missing_body_does_not_crash(self):
        for name in ('report_progress', 'ask_supervisor', 'finish_task'):
            self.assertTrue(AgentManager._format_comm_tool_text(name, {}))
            self.assertTrue(AgentManager._format_comm_tool_text(name, None))


class CommToolControlFlowTests(unittest.TestCase):
    def _manager(self):
        manager = object.__new__(AgentManager)
        manager.emitted = []
        manager.emit_progress_report = lambda agent_id, text: manager.emitted.append((agent_id, text))
        return manager

    def test_progress_does_not_touch_exit_intent(self):
        manager = self._manager()
        intent: dict = {}

        result = manager._apply_agent_comm_tool('a1', 'report_progress', {'text': '进行中'}, intent)

        # 汇报不该中断回合，否则 agent 每同步一次就要被上级重新唤醒
        self.assertEqual({}, intent)
        self.assertEqual(1, len(manager.emitted))
        self.assertIn('继续', result)

    def test_ask_supervisor_requests_waiting(self):
        manager = self._manager()
        intent: dict = {}

        manager._apply_agent_comm_tool('a1', 'ask_supervisor', {'question': '方案A还是B'}, intent)

        self.assertEqual('waiting', intent['kind'])
        self.assertEqual('ask_supervisor', intent['tool'])
        self.assertIn('方案A还是B', intent['text'])
        self.assertEqual([], manager.emitted)

    def test_finish_task_requests_idle_and_keeps_raw_summary(self):
        manager = self._manager()
        intent: dict = {}

        manager._apply_agent_comm_tool('a1', 'finish_task', {'summary': '全部改完'}, intent)

        self.assertEqual('idle', intent['kind'])
        # body 保留原始 summary：失败判定要看正文，渲染后的首行是固定抬头
        self.assertEqual('全部改完', intent['body'])

    def test_second_exit_call_keeps_first_intent(self):
        manager = self._manager()
        intent: dict = {}

        manager._apply_agent_comm_tool('a1', 'ask_supervisor', {'question': '先问'}, intent)
        result = manager._apply_agent_comm_tool('a1', 'finish_task', {'summary': '又说完成'}, intent)

        self.assertEqual('waiting', intent['kind'])
        self.assertIn('先问', intent['text'])
        self.assertIn('ask_supervisor', result)

    def test_progress_still_allowed_after_exit_intent(self):
        manager = self._manager()
        intent = {'kind': 'idle', 'tool': 'finish_task', 'text': 'done', 'body': 'done'}

        manager._apply_agent_comm_tool('a1', 'report_progress', {'text': '补一句'}, intent)

        self.assertEqual('idle', intent['kind'])
        self.assertEqual(1, len(manager.emitted))


class FinishTaskFailureDetectionTests(unittest.TestCase):
    def test_failure_summary_is_detected_from_raw_body(self):
        manager = object.__new__(AgentManager)
        intent: dict = {}
        manager.emit_progress_report = lambda *_a: None
        manager._apply_agent_comm_tool('a1', 'finish_task', {'summary': '失败：依赖装不上，无法继续'}, intent)

        # 渲染文本首行是【agent 完成】，只有对着 body 判定才能识破假完成
        self.assertTrue(manager._looks_terminal_failure(intent['body']))
        self.assertFalse(manager._looks_terminal_failure(intent['text']))


if __name__ == '__main__':
    unittest.main()
