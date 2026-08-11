import unittest

from core.ai_runtime import AIOrchestrator


class _Runtime:
    """只搭出 _describe_executed_tools 需要的最小状态。"""

    def __new__(cls, executed=None):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_executed_tools = dict(executed or {})
        return runtime


class DescribeExecutedToolsTests(unittest.TestCase):
    def _describe(self, names):
        runtime = _Runtime({'group:7': list(names)})
        return runtime._describe_executed_tools('group', '7')

    def test_empty_when_nothing_ran(self):
        self.assertEqual('', self._describe([]))

    def test_unknown_scope_is_empty(self):
        runtime = _Runtime({'group:7': ['destroy_agent']})
        self.assertEqual('', runtime._describe_executed_tools('group', '8'))

    def test_single_tool_has_no_count_suffix(self):
        self.assertEqual('list_agents', self._describe(['list_agents']))

    def test_repeated_tool_is_counted(self):
        # 58 次 destroy_agent 挤在一轮里，摘要必须显示次数而不是刷 58 遍
        self.assertEqual('destroy_agent x3', self._describe(['destroy_agent'] * 3))

    def test_most_frequent_tool_comes_first(self):
        summary = self._describe(['destroy_agent'] * 25 + ['list_agents'])
        self.assertTrue(summary.startswith('destroy_agent x25'), summary)
        self.assertIn('list_agents', summary)

    def test_blank_names_are_dropped(self):
        self.assertEqual('list_agents', self._describe(['', None, 'list_agents']))

    def test_all_blank_is_empty(self):
        self.assertEqual('', self._describe(['', None]))


class ExecutedToolResetTests(unittest.TestCase):
    def test_scope_key_is_isolated(self):
        runtime = _Runtime({'group:7': ['destroy_agent'], 'private:7': ['send_message']})
        self.assertEqual('destroy_agent', runtime._describe_executed_tools('group', '7'))
        self.assertEqual('send_message', runtime._describe_executed_tools('private', '7'))


class _Msg:
    chat_type = 'group'
    chat_id = '7'


class _Repo:
    def __init__(self):
        self.appended = []

    def append_message(self, scope_type, scope_id, entry, *_a, **_kw):
        self.appended.append((scope_type, scope_id, entry))


class _Bot:
    self_id = 1

    def __init__(self):
        self.private = []

    def send_private_text(self, uid, text):
        self.private.append((uid, text))


class _Cfg:
    history_limit = 50
    diary_size = 10


class _Models:
    @staticmethod
    def get_current_model():
        return {'display_name': 'Cursor Claude/claude-opus-5'}


class InterruptHandlerTests(unittest.IsolatedAsyncioTestCase):
    """真正驱动 _run_message_turn 的异常分支，而不是复刻它的判定逻辑。"""

    def _runtime(self, exc, executed):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_executed_tools = {'group:7': list(executed)}
        runtime.repo = _Repo()
        runtime.bot = _Bot()
        runtime.config = _Cfg()
        runtime.model_manager = _Models()

        async def _boom(_item):
            raise exc

        runtime._process_message = _boom
        runtime._is_epoch_stale = lambda _e: False
        runtime._has_completed_turn_commit = lambda _i: False
        runtime._merge_followup_after_turn = lambda _i, _c: None
        return runtime

    async def _run(self, exc, executed=()):
        runtime = self._runtime(exc, executed)
        await runtime._run_message_turn({'message': _Msg()})
        note = runtime.repo.appended[0][2]['text'] if runtime.repo.appended else ''
        return note, runtime.bot.private

    async def test_note_records_executed_tools(self):
        note, _ = await self._run(RuntimeError('模型返回空内容'), ['destroy_agent'] * 25)
        self.assertIn('destroy_agent x25', note)
        self.assertIn('已经执行完并生效', note)

    async def test_note_warns_against_repeating(self):
        note, _ = await self._run(RuntimeError('模型返回空内容'), ['destroy_agent'])
        self.assertIn('不要重复执行', note)

    async def test_note_stays_short_when_no_tools_ran(self):
        note, _ = await self._run(RuntimeError('模型返回空内容'))
        self.assertIn('异常中断', note)
        self.assertNotIn('已经执行完并生效', note)

    async def test_soft_error_with_tools_notifies_owner(self):
        # 这就是 58 个 agent 被静默删掉的那一幕
        _, private = await self._run(RuntimeError('模型返回空内容'), ['destroy_agent'] * 25)
        self.assertEqual(1, len(private))
        self.assertIn('destroy_agent x25', private[0][1])

    async def test_soft_error_without_tools_stays_silent(self):
        _, private = await self._run(RuntimeError('模型返回空内容'))
        self.assertEqual([], private)

    async def test_timeout_with_tools_notifies_owner(self):
        _, private = await self._run(RuntimeError('read timed out'), ['destroy_agent'])
        self.assertEqual(1, len(private))

    async def test_timeout_without_tools_stays_silent(self):
        _, private = await self._run(RuntimeError('read timed out'))
        self.assertEqual([], private)

    async def test_hard_error_notifies_even_without_tools(self):
        _, private = await self._run(RuntimeError('status=401 unauthorized'))
        self.assertEqual(1, len(private))

    async def test_owner_notice_omits_tool_line_when_nothing_ran(self):
        _, private = await self._run(RuntimeError('status=401 unauthorized'))
        self.assertNotIn('中断前已生效的工具', private[0][1])


if __name__ == '__main__':
    unittest.main()
