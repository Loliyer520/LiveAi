import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_repository import AIRepository
from core.ai_runtime import AIOrchestrator
from pack.json_store import JsonStore


def _make_repo(tmpdir: str) -> AIRepository:
    store = JsonStore(str(Path(tmpdir) / 'state.json'))
    return AIRepository(store=store)


def _make_runtime(repo: AIRepository):
    """只搭出 _execute_live_action_tool_call send_message 分支需要的状态。

    真实 repo 落库;网络发送用 _send_scope_message 替身,保证测试不碰真实 QQ。
    """
    runtime = object.__new__(AIOrchestrator)
    runtime.bot = SimpleNamespace(self_id=10001)
    runtime.repo = repo
    runtime.config = SimpleNamespace(history_limit=20, diary_size=10)
    runtime.tools = Mock()
    runtime.tools.record_tool_use = Mock()
    runtime._normalize_think_note = AIOrchestrator._normalize_think_note.__get__(runtime, AIOrchestrator)
    runtime._normalize_tool_context_messages = AIOrchestrator._normalize_tool_context_messages.__get__(runtime, AIOrchestrator)
    runtime._build_outbound_message_entry = AIOrchestrator._build_outbound_message_entry.__get__(runtime, AIOrchestrator)
    runtime._append_outbound_message_now = AIOrchestrator._append_outbound_message_now.__get__(runtime, AIOrchestrator)
    runtime._strip_send_message_thinking = AIOrchestrator._strip_send_message_thinking.__get__(runtime, AIOrchestrator)
    runtime._split_long_reply_lines = AIOrchestrator._split_long_reply_lines.__get__(runtime, AIOrchestrator)
    runtime._normalize_live_send_action_key = AIOrchestrator._normalize_live_send_action_key.__get__(runtime, AIOrchestrator)
    runtime._normalize_message_ref = lambda value: value
    runtime._lookup_message_ref = Mock(return_value=None)
    runtime._register_turn_message_ref = lambda _scope_type, _scope_id, entry: dict(entry)
    runtime._persona_notice_scope_key = AIOrchestrator._persona_notice_scope_key.__get__(runtime, AIOrchestrator)
    runtime._mark_send_message_persona_notice = AIOrchestrator._mark_send_message_persona_notice.__get__(runtime, AIOrchestrator)
    return runtime


def _send(runtime, content: str) -> None:
    sent_entries = []
    checkpointed = []
    runtime._send_scope_message = Mock(
        side_effect=lambda _message, _content, on_sent_entry=None: (
            on_sent_entry({'text': _content, 'message_id': 'm1'}),
            [{'text': _content, 'message_id': 'm1'}],
        )[-1]
    )
    runtime._execute_live_action_tool_call(
        'private',
        '7',
        'agent-1',
        SimpleNamespace(chat_type='private', chat_id='7'),
        SimpleNamespace(name='send_message', input={'content': content}),
        None,
        True,
        True,
        sent_entries,
        checkpointed,
    )


class AiSeesOwnSendsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = _make_repo(self._tmp.name)

    def test_send_message_is_persisted_to_repo_at_send_time(self):
        runtime = _make_runtime(self.repo)
        _send(runtime, '我刚买到票了')
        messages = self.repo.list_messages('private', '7')
        self.assertEqual(1, len(messages))
        self.assertEqual('我刚买到票了', messages[0]['text'])
        self.assertEqual(10001, messages[0]['user_id'], '落库条目必须带 bot 自身 user_id')

    def test_next_turn_history_renders_own_send_as_assistant(self):
        runtime = _make_runtime(self.repo)
        _send(runtime, '我刚买到票了')
        history = runtime._flatten_diary_context(self.repo.get_diary_context('private', '7'))
        rendered = AIOrchestrator._build_role_based_history_messages(runtime, history)
        roles = [m['role'] for m in rendered]
        self.assertIn('assistant', roles)
        self.assertEqual('我刚买到票了', rendered[-1]['content'])

    def test_own_send_survives_diary_rollover(self):
        runtime = _make_runtime(self.repo)
        for i in range(12):
            _send(runtime, f'第 {i} 条')
        history = runtime._flatten_diary_context(self.repo.get_diary_context('private', '7'))
        rendered = AIOrchestrator._build_role_based_history_messages(runtime, history)
        texts = [m['content'] for m in rendered if m['role'] == 'assistant']
        self.assertIn('第 11 条', texts, '发送即落库的消息必须在后续轮次可见')

    def test_interleaved_user_and_own_sends_keep_order(self):
        runtime = _make_runtime(self.repo)
        runtime.repo.append_message(
            'private', '7',
            {'user_id': 42, 'nickname': '小明', 'text': '帮我买张票', 'timestamp': 1000.0},
            runtime.config.history_limit,
            runtime.config.diary_size,
        )
        _send(runtime, '好，正在买')
        history = runtime._flatten_diary_context(self.repo.get_diary_context('private', '7'))
        rendered = AIOrchestrator._build_role_based_history_messages(runtime, history)
        self.assertEqual('user', rendered[0]['role'])
        self.assertEqual('assistant', rendered[1]['role'])
        self.assertEqual('好，正在买', rendered[1]['content'])


if __name__ == '__main__':
    unittest.main()
