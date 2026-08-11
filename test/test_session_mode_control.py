import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.ai_runtime import CODE_MODE_TOOL_NAMES, AIOrchestrator


def _iterations(*names):
    return [{'tool_calls': [{'name': n} for n in names]}]


class CodeModeToolSetTests(unittest.TestCase):
    def test_set_is_derived_and_non_empty(self):
        self.assertIn('create_agent', CODE_MODE_TOOL_NAMES)
        self.assertIn('web_search', CODE_MODE_TOOL_NAMES)
        self.assertIn('memory_list', CODE_MODE_TOOL_NAMES)

    def test_chat_tools_are_excluded(self):
        for name in ('send_message', 'stay_silent', 'notify_master', 'relation_lookup'):
            self.assertNotIn(name, CODE_MODE_TOOL_NAMES)

    def test_set_session_mode_does_not_count_as_work(self):
        # 否则一调用就清零，永远等不到下一次提示
        self.assertNotIn('set_session_mode', CODE_MODE_TOOL_NAMES)


class IdleCounterTests(unittest.TestCase):
    def _runtime(self, mode=None):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_session_modes = {}
        runtime._scope_code_idle_turns = {}
        if mode:
            runtime._scope_session_modes['group:7'] = mode
        return runtime

    def _idle(self, runtime):
        return runtime._scope_code_idle_turns.get('group:7', 0)

    def test_chat_mode_never_accumulates(self):
        runtime = self._runtime('chat')
        runtime._note_session_mode_activity('group', '7', [])
        self.assertEqual(0, self._idle(runtime))

    def test_idle_turn_increments(self):
        runtime = self._runtime('code')
        runtime._note_session_mode_activity('group', '7', _iterations('send_message'))
        runtime._note_session_mode_activity('group', '7', None)
        self.assertEqual(2, self._idle(runtime))

    def test_code_tool_resets(self):
        runtime = self._runtime('code')
        for _ in range(5):
            runtime._note_session_mode_activity('group', '7', None)
        runtime._note_session_mode_activity('group', '7', _iterations('send_message', 'create_agent'))
        self.assertEqual(0, self._idle(runtime))

    def test_internal_turns_do_not_count(self):
        # agent 汇报轮不是对话轮，不该把会话推向"该切回 chat"
        runtime = self._runtime('code')
        runtime._note_session_mode_activity('group', '7', None, 'agent_report')
        self.assertEqual(0, self._idle(runtime))

    def test_switching_mode_resets_counter(self):
        runtime = self._runtime('code')
        for _ in range(25):
            runtime._note_session_mode_activity('group', '7', None)
        runtime._set_scope_session_mode('group', '7', 'code')
        self.assertEqual(0, self._idle(runtime))


class SwitchHintTests(unittest.TestCase):
    def _runtime(self, mode='code', idle=0):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_session_modes = {'group:7': mode}
        runtime._scope_code_idle_turns = {'group:7': idle}
        return runtime

    def test_no_hint_below_limit(self):
        runtime = self._runtime(idle=19)
        self.assertEqual('', runtime._consume_code_mode_switch_hint('group', '7'))

    def test_hint_at_limit(self):
        runtime = self._runtime(idle=20)
        hint = runtime._consume_code_mode_switch_hint('group', '7')
        self.assertIn('set_session_mode', hint)
        self.assertIn('chat', hint)
        self.assertIn('不要发给用户', hint)

    def test_hint_is_consumed_once(self):
        runtime = self._runtime(idle=30)
        self.assertTrue(runtime._consume_code_mode_switch_hint('group', '7'))
        # 取走即归零，否则每轮都念
        self.assertEqual('', runtime._consume_code_mode_switch_hint('group', '7'))

    def test_chat_mode_gets_no_hint(self):
        runtime = self._runtime(mode='chat', idle=99)
        self.assertEqual('', runtime._consume_code_mode_switch_hint('group', '7'))

    def test_hint_is_per_scope(self):
        runtime = self._runtime(idle=25)
        runtime._scope_session_modes['group:8'] = 'code'
        runtime._scope_code_idle_turns['group:8'] = 0
        self.assertTrue(runtime._consume_code_mode_switch_hint('group', '7'))
        self.assertEqual('', runtime._consume_code_mode_switch_hint('group', '8'))

    def test_limit_is_twenty(self):
        self.assertEqual(20, AIOrchestrator.CODE_MODE_IDLE_TURN_LIMIT)


class DefaultModeTests(unittest.TestCase):
    def test_default_is_chat(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_session_modes = {}
        self.assertEqual('chat', runtime._get_scope_session_mode('group', '7'))
        self.assertEqual('chat', runtime._get_scope_session_mode('private', '1'))


class CodeModeReadToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, mode):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_session_modes = {'group:7': mode}
        runtime.tools = SimpleNamespace(record_tool_use=Mock())
        runtime.config = SimpleNamespace(history_limit=20)
        return runtime

    async def test_chat_mode_rejects_direct_read_tool_call(self):
        runtime = self._runtime('chat')

        result = await runtime._run_ai_tool_call(
            'group', '7', 'group:7', 'read_local_file', {'path': 'README.md'}
        )

        self.assertIn('只在 code 模式下可用', result)

    async def test_code_mode_executes_all_read_tools(self):
        runtime = self._runtime('code')
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'core'))
            with open(os.path.join(root, 'core', 'sample.py'), 'w', encoding='utf-8') as handle:
                handle.write('TARGET = 1\n')
            with patch('core.ai_runtime._project_root', return_value=root):
                found = await runtime._run_ai_tool_call(
                    'group', '7', 'group:7', 'find_in_project',
                    {'content_query': 'TARGET'},
                )
                listed = await runtime._run_ai_tool_call(
                    'group', '7', 'group:7', 'list_local_files', {'subpath': 'core'}
                )
                content = await runtime._run_ai_tool_call(
                    'group', '7', 'group:7', 'read_local_file', {'path': 'core/sample.py'}
                )

        self.assertIn('core/sample.py:1', found)
        self.assertIn('sample.py', listed)
        self.assertEqual('TARGET = 1\n', content)


if __name__ == '__main__':
    unittest.main()
