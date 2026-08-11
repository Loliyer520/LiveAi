import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.ai_runtime import AIOrchestrator
from core.event_batch_coordinator import AtomicTurnBatchCoordinator
from core.event_mailbox import InMemoryEventMailbox


class LiveToolCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def test_build_role_based_history_messages_moves_legacy_tool_checkpoint_before_ai_text(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(self_id=10001)
        runtime._normalize_tool_context_messages = (
            AIOrchestrator._normalize_tool_context_messages.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._render_pending_user_segment = Mock(return_value='user hi')

        messages = AIOrchestrator._build_role_based_history_messages(
            runtime,
            [
                {'user_id': '42', 'text': 'hi'},
                {'user_id': '10001', 'text': '第一行'},
                {'user_id': '10001', 'text': '第二行'},
                {
                    'user_id': '10001',
                    'text': '',
                    'tool_context_messages': [
                        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-1'}]},
                        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-1'}]},
                    ],
                },
            ],
        )

        self.assertEqual(messages[0], {'role': 'user', 'content': 'user hi'})
        self.assertEqual(messages[1]['role'], 'assistant')
        self.assertEqual(messages[1]['content'][0]['type'], 'tool_use')
        self.assertEqual(messages[2]['role'], 'user')
        self.assertEqual(messages[2]['content'][0]['type'], 'tool_result')
        self.assertEqual(messages[3]['content'], '第一行')
        self.assertEqual(messages[4]['content'], '第二行')

    def test_build_role_based_history_messages_prefers_latest_legacy_tool_checkpoint(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(self_id=10001)
        runtime._normalize_tool_context_messages = (
            AIOrchestrator._normalize_tool_context_messages.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._render_pending_user_segment = Mock(return_value='user hi')

        messages = AIOrchestrator._build_role_based_history_messages(
            runtime,
            [
                {'user_id': '42', 'text': 'hi'},
                {
                    'user_id': '10001',
                    'text': '',
                    'tool_context_messages': [
                        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-1'}]},
                        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-1'}]},
                    ],
                },
                {
                    'user_id': '10001',
                    'text': '',
                    'tool_context_messages': [
                        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-2'}]},
                        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-2'}]},
                    ],
                },
                {'user_id': '10001', 'text': '最终回复'},
            ],
        )

        self.assertEqual(messages[1]['content'][0]['id'], 'call-2')
        self.assertEqual(messages[2]['content'][0]['tool_use_id'], 'call-2')
        self.assertEqual(messages[3]['content'], '最终回复')

    def test_append_live_tool_checkpoint_persists_tool_context_only_entry(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(self_id=10001)
        runtime.repo = Mock()
        runtime.config = SimpleNamespace(history_limit=20, diary_size=10)
        runtime._normalize_think_note = AIOrchestrator._normalize_think_note.__get__(
            runtime, AIOrchestrator
        )
        runtime._normalize_tool_context_messages = (
            AIOrchestrator._normalize_tool_context_messages.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._build_outbound_message_entry = (
            AIOrchestrator._build_outbound_message_entry.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._build_live_tool_checkpoint_entry = (
            AIOrchestrator._build_live_tool_checkpoint_entry.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime.repo.upsert_tool_context_checkpoint.side_effect = (
            lambda scope_type, scope_id, checkpoint_id, message, *_args: (message, False)
        )

        entry = runtime._append_live_tool_checkpoint(
            'group',
            '7',
            'checkpoint-1',
            [
                {
                    'role': 'assistant',
                    'content': [
                        {
                            'type': 'tool_use',
                            'id': 'call-1',
                            'name': 'send_message',
                            'input': {'content': '你好'},
                        }
                    ],
                },
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'tool_result',
                            'tool_use_id': 'call-1',
                            'content': '已发送 1 条消息。',
                        }
                    ],
                },
            ],
        )

        self.assertEqual(entry['text'], '')
        self.assertEqual(entry['tool_checkpoint_id'], 'checkpoint-1')
        args = runtime.repo.upsert_tool_context_checkpoint.call_args.args
        self.assertEqual(args[0:3], ('group', '7', 'checkpoint-1'))
        self.assertEqual(args[3]['text'], '')
        self.assertIn('tool_context_messages', args[3])
        self.assertEqual(args[3]['tool_context_messages'][0]['role'], 'assistant')
        self.assertEqual(args[3]['tool_context_messages'][1]['role'], 'user')

    def _make_process_runtime(self, bundle):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(enabled=True)
        runtime._message_epoch = 1
        runtime._turn_image_refs = {}
        runtime._event_mailbox = InMemoryEventMailbox()
        runtime._turn_batch_coordinator = AtomicTurnBatchCoordinator(runtime._event_mailbox)
        runtime.repo = Mock()
        runtime.repo.get_or_create_agent = Mock(
            return_value=SimpleNamespace(
                persona='persona',
                impression='',
                display_name='display',
            )
        )
        runtime.repo.get_diary_context = Mock(
            return_value={
                'summaries': [],
                'window': [],
                'pending': [],
                'current': [],
            }
        )
        runtime._message_source_label = lambda _message: 'private'
        runtime._maybe_resolve_display_name = lambda *_args: None
        runtime._flatten_diary_context = lambda _ctx: []
        runtime._build_global_identity_context_for_message = (
            lambda *_args: ''
        )
        runtime._scope_key = lambda *_args: 'private:1'
        runtime._extract_image_refs = lambda _raw: []
        runtime._build_group_context = AsyncMock(return_value='')
        runtime._build_child_messages = lambda *_args, **_kwargs: {
            'system': [],
            'messages': [],
        }
        runtime._complete_child_turn = AsyncMock(return_value=(bundle, 12, False))
        runtime._is_epoch_stale = lambda _epoch: False
        runtime._finalize_reply = lambda _message, reply: reply
        runtime._scope_session_modes = {}
        runtime._record_outbound_message = AsyncMock(return_value={'text': 'ok'})
        return runtime

    async def test_enqueue_message_ignores_system_private_before_persisting_history(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.repo = Mock()
        runtime.bot = SimpleNamespace(self_id=10001)
        runtime._is_message_allowed_by_power_mode = lambda _message: True
        runtime._message_scope = lambda _message: ('private', '7')
        runtime._message_source_label = lambda _message: 'system_private'
        runtime._message_source_kind = lambda _message: 'system_private'
        runtime._should_ignore_message = lambda _message: True
        runtime._clean_text = lambda _message: _message.text

        message = SimpleNamespace(
            chat_type='private',
            chat_id='7',
            user_id='42',
            nickname='tester',
            text='系统消息',
            raw_message='系统消息',
            message_id='m-1',
            is_private=True,
            mentions_self=False,
        )

        await AIOrchestrator._enqueue_message(runtime, message)

        runtime.repo.append_message.assert_not_called()
        runtime.repo.touch_user_identity.assert_not_called()
        runtime.repo.get_or_create_agent.assert_not_called()
        runtime.repo.add_note.assert_called_once()

    async def test_process_message_skips_duplicate_tool_context_write_after_checkpoint(self):
        bundle = {
            'message': '已经发出',
            'think_note': '',
            'tool_context_messages': [
                {'role': 'assistant', 'content': 'shadow'},
                {'role': 'user', 'content': 'result'},
            ],
            'live_tool_context_checkpointed': True,
        }
        runtime = self._make_process_runtime(bundle)
        message = SimpleNamespace(
            chat_type='private',
            chat_id='1',
            nickname='tester',
            user_id='42',
            text='hi',
            raw_message='hi',
            message_id='m-1',
        )

        item = {
            'message_epoch': 1,
            'message': message,
            'cleaned': 'hi',
            'agent_id': 'agent-1',
            'trigger_messages': [{'text': 'hi', 'raw_message': 'hi'}],
            'deferred_count': 0,
        }

        await runtime._process_message(item)

        self.assertTrue(runtime._record_outbound_message.await_count >= 1)
        self.assertIsNone(
            runtime._record_outbound_message.await_args.kwargs['tool_context_messages']
        )

    async def test_process_message_skips_aggregate_outbound_write_when_live_entries_exist(self):
        bundle = {
            'message': '第一行\n第二行',
            'think_note': '',
            'tool_context_messages': [],
            'live_tool_context_checkpointed': True,
            'live_outbound_entries': [
                {'text': '第一行', 'raw_message': '第一行', 'message_id': '1', 'timestamp': 1.0},
                {'text': '第二行', 'raw_message': '第二行', 'message_id': '2', 'timestamp': 2.0},
            ],
        }
        runtime = self._make_process_runtime(bundle)
        message = SimpleNamespace(
            chat_type='private',
            chat_id='1',
            nickname='tester',
            user_id='42',
            text='hi',
            raw_message='hi',
            message_id='m-1',
        )
        item = {
            'message_epoch': 1,
            'message': message,
            'cleaned': 'hi',
            'agent_id': 'agent-1',
            'trigger_messages': [{'text': 'hi', 'raw_message': 'hi'}],
            'deferred_count': 0,
        }

        await runtime._process_message(item)

        runtime._record_outbound_message.assert_not_awaited()
        self.assertEqual(
            [entry['text'] for entry in item['followup_history_seed'][-2:]],
            ['第一行', '第二行'],
        )

    async def test_process_message_attaches_tool_context_to_first_live_entry(self):
        bundle = {
            'message': '第一行\n第二行',
            'think_note': '',
            'tool_context_messages': [
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-1'}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-1'}]},
            ],
            'live_tool_context_checkpointed': False,
            'live_outbound_entries': [
                {'text': '第一行', 'raw_message': '第一行', 'message_id': '1', 'timestamp': 1.0},
                {'text': '第二行', 'raw_message': '第二行', 'message_id': '2', 'timestamp': 2.0},
            ],
        }
        runtime = self._make_process_runtime(bundle)
        runtime.repo.attach_tool_context_to_message = Mock(return_value=True)
        message = SimpleNamespace(
            chat_type='private',
            chat_id='1',
            nickname='tester',
            user_id='42',
            text='hi',
            raw_message='hi',
            message_id='m-1',
        )
        item = {
            'message_epoch': 1,
            'message': message,
            'cleaned': 'hi',
            'agent_id': 'agent-1',
            'trigger_messages': [{'text': 'hi', 'raw_message': 'hi'}],
            'deferred_count': 0,
        }

        await runtime._process_message(item)

        runtime._record_outbound_message.assert_not_awaited()
        runtime.repo.attach_tool_context_to_message.assert_called_once()
        self.assertIn('tool_context_messages', item['followup_history_seed'][-2])
        self.assertEqual(item['followup_history_seed'][-2]['tool_context_messages'][0]['role'], 'assistant')

    async def test_process_message_does_not_rewrite_empty_checkpoint_only_turn(self):
        bundle = {
            'message': '',
            'think_note': '',
            'tool_context_messages': [
                {'role': 'assistant', 'content': 'shadow'},
                {'role': 'user', 'content': 'result'},
            ],
            'live_tool_context_checkpointed': True,
            'live_tool_checkpoint_entry': {
                'text': '',
                'tool_context_messages': [
                    {'role': 'assistant', 'content': 'shadow'},
                    {'role': 'user', 'content': 'result'},
                ],
            },
        }
        runtime = self._make_process_runtime(bundle)
        message = SimpleNamespace(
            chat_type='private',
            chat_id='1',
            nickname='tester',
            user_id='42',
            text='hi',
            raw_message='hi',
            message_id='m-1',
        )

        item = {
            'message_epoch': 1,
            'message': message,
            'cleaned': 'hi',
            'agent_id': 'agent-1',
            'trigger_messages': [{'text': 'hi', 'raw_message': 'hi'}],
            'deferred_count': 0,
        }

        await runtime._process_message(item)

        runtime._record_outbound_message.assert_not_awaited()
        self.assertEqual(item['followup_history_seed'][-1]['text'], '')
        self.assertIn('tool_context_messages', item['followup_history_seed'][-1])

    def test_live_send_message_checkpoints_each_sent_entry_immediately(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(self_id=10001)
        runtime.repo = Mock()
        runtime.config = SimpleNamespace(history_limit=20, diary_size=10)
        runtime._normalize_think_note = AIOrchestrator._normalize_think_note.__get__(
            runtime, AIOrchestrator
        )
        runtime._normalize_tool_context_messages = (
            AIOrchestrator._normalize_tool_context_messages.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._build_outbound_message_entry = (
            AIOrchestrator._build_outbound_message_entry.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._append_outbound_message_now = Mock()
        runtime.tools = Mock()
        runtime.tools.record_tool_use = Mock()

        runtime._send_scope_message = Mock(
            side_effect=lambda _message, _content, on_sent_entry=None: (
                on_sent_entry({'text': '第一行', 'message_id': '1'}),
                on_sent_entry({'text': '第二行', 'message_id': '2'}),
                [
                    {'text': '第一行', 'message_id': '1'},
                    {'text': '第二行', 'message_id': '2'},
                ],
            )[-1]
        )

        sent_entries = []
        checkpointed = []
        result = runtime._execute_live_action_tool_call(
            'private',
            '7',
            'agent-1',
            SimpleNamespace(chat_type='private', chat_id='7'),
            SimpleNamespace(name='send_message', input={'content': '第一行\n第二行'}),
            None,
            True,
            True,
            sent_entries,
            checkpointed,
        )

        self.assertEqual([entry['text'] for entry in sent_entries], ['第一行', '第二行'])
        self.assertEqual([entry['text'] for entry in checkpointed], ['第一行', '第二行'])
        self.assertEqual(runtime._append_outbound_message_now.call_count, 2)
        self.assertIn('已发送 2 条消息', result)

    def test_live_send_message_marks_uncommitted_checkpoint_when_history_append_fails(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(self_id=10001)
        runtime.repo = Mock()
        runtime.config = SimpleNamespace(history_limit=20, diary_size=10)
        runtime._normalize_think_note = AIOrchestrator._normalize_think_note.__get__(
            runtime, AIOrchestrator
        )
        runtime._normalize_tool_context_messages = (
            AIOrchestrator._normalize_tool_context_messages.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._build_outbound_message_entry = (
            AIOrchestrator._build_outbound_message_entry.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._append_outbound_message_now = Mock(
            side_effect=[None, RuntimeError('persist failed')]
        )
        runtime.tools = Mock()
        runtime.tools.record_tool_use = Mock()

        runtime._send_scope_message = Mock(
            side_effect=lambda _message, _content, on_sent_entry=None: (
                on_sent_entry({'text': '第一行', 'message_id': '1'}),
                on_sent_entry({'text': '第二行', 'message_id': '2'}),
                [
                    {'text': '第一行', 'message_id': '1'},
                    {'text': '第二行', 'message_id': '2'},
                ],
            )[-1]
        )

        sent_entries = []
        checkpointed = []
        runtime._execute_live_action_tool_call(
            'private',
            '7',
            'agent-1',
            SimpleNamespace(chat_type='private', chat_id='7'),
            SimpleNamespace(name='send_message', input={'content': '第一行\n第二行'}),
            None,
            True,
            True,
            sent_entries,
            checkpointed,
        )

        self.assertEqual([entry['text'] for entry in sent_entries], ['第一行', '第二行'])
        self.assertEqual([entry['text'] for entry in checkpointed], ['第一行', '第二行'])
        self.assertTrue(checkpointed[0]['_history_committed'])
        self.assertFalse(checkpointed[1]['_history_committed'])
        self.assertEqual(runtime._append_outbound_message_now.call_count, 2)

    def test_live_send_message_blocks_duplicate_payload_within_same_turn(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(self_id=10001)
        runtime.repo = Mock()
        runtime.config = SimpleNamespace(history_limit=20, diary_size=10)
        runtime._normalize_think_note = AIOrchestrator._normalize_think_note.__get__(
            runtime, AIOrchestrator
        )
        runtime._normalize_tool_context_messages = (
            AIOrchestrator._normalize_tool_context_messages.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._normalize_live_send_action_key = (
            AIOrchestrator._normalize_live_send_action_key.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._strip_send_message_thinking = (
            AIOrchestrator._strip_send_message_thinking.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._split_long_reply_lines = lambda text: text
        runtime._append_outbound_message_now = Mock()
        runtime.tools = Mock()
        runtime.tools.record_tool_use = Mock()
        runtime._normalize_message_ref = lambda value: value
        runtime._lookup_message_ref = Mock(return_value=None)
        runtime._register_turn_message_ref = lambda *_args: dict(_args[-1])
        runtime._send_scope_message = Mock(
            side_effect=lambda _message, _content, on_sent_entry=None: (
                on_sent_entry({'text': '重复回复', 'message_id': '1'}),
                [{'text': '重复回复', 'message_id': '1'}],
            )[-1]
        )

        sent_entries = []
        checkpointed = []
        ledger = set()
        first = runtime._execute_live_action_tool_call(
            'private',
            '7',
            'agent-1',
            SimpleNamespace(chat_type='private', chat_id='7'),
            SimpleNamespace(name='send_message', input={'content': '重复回复'}),
            None,
            True,
            True,
            sent_entries,
            checkpointed,
            live_send_action_ledger=ledger,
        )
        second = runtime._execute_live_action_tool_call(
            'private',
            '7',
            'agent-1',
            SimpleNamespace(chat_type='private', chat_id='7'),
            SimpleNamespace(name='send_message', input={'content': '重复回复'}),
            None,
            True,
            True,
            sent_entries,
            checkpointed,
            live_send_action_ledger=ledger,
        )

        self.assertIn('已发送 1 条消息', first)
        self.assertIn('拦截', second)
        self.assertIn('stay_silent', second)
        runtime._send_scope_message.assert_called_once()

    def test_live_send_message_keeps_partial_checkpoint_when_send_breaks_midway(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(self_id=10001)
        runtime.repo = Mock()
        runtime.config = SimpleNamespace(history_limit=20, diary_size=10)
        runtime._normalize_think_note = AIOrchestrator._normalize_think_note.__get__(
            runtime, AIOrchestrator
        )
        runtime._normalize_tool_context_messages = (
            AIOrchestrator._normalize_tool_context_messages.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._build_outbound_message_entry = (
            AIOrchestrator._build_outbound_message_entry.__get__(
                runtime, AIOrchestrator
            )
        )
        runtime._append_outbound_message_now = Mock()
        runtime.tools = Mock()

        def broken_send(_message, _content, on_sent_entry=None):
            on_sent_entry({'text': '第一行', 'message_id': '1'})
            raise RuntimeError('line 2 failed')

        runtime._send_scope_message = Mock(side_effect=broken_send)

        sent_entries = []
        checkpointed = []
        with self.assertRaisesRegex(RuntimeError, 'line 2 failed'):
            runtime._execute_live_action_tool_call(
                'private',
                '7',
                'agent-1',
                SimpleNamespace(chat_type='private', chat_id='7'),
                SimpleNamespace(name='send_message', input={'content': '第一行\n第二行'}),
                None,
                True,
                True,
                sent_entries,
                checkpointed,
            )

        self.assertEqual([entry['text'] for entry in sent_entries], ['第一行'])
        self.assertEqual([entry['text'] for entry in checkpointed], ['第一行'])
        runtime._append_outbound_message_now.assert_called_once()

    async def test_process_message_recovers_uncommitted_live_entries(self):
        bundle = {
            'message': '第一行\n第二行',
            'think_note': '',
            'tool_context_messages': [],
            'live_tool_context_checkpointed': True,
            'live_outbound_entries': [
                {
                    'text': '第一行',
                    'raw_message': '第一行',
                    'message_id': '1',
                    'timestamp': 1.0,
                    '_history_committed': True,
                },
                {
                    'text': '第二行',
                    'raw_message': '第二行',
                    'message_id': '2',
                    'timestamp': 2.0,
                    '_history_committed': False,
                },
            ],
        }
        runtime = self._make_process_runtime(bundle)
        runtime._append_outbound_message_now = Mock()
        message = SimpleNamespace(
            chat_type='private',
            chat_id='1',
            nickname='tester',
            user_id='42',
            text='hi',
            raw_message='hi',
            message_id='m-1',
        )
        item = {
            'message_epoch': 1,
            'message': message,
            'cleaned': 'hi',
            'agent_id': 'agent-1',
            'trigger_messages': [{'text': 'hi', 'raw_message': 'hi'}],
            'deferred_count': 0,
        }

        await runtime._process_message(item)

        runtime._record_outbound_message.assert_not_awaited()
        runtime._append_outbound_message_now.assert_called_once()
        recovered_entry = runtime._append_outbound_message_now.call_args.args[2]
        self.assertEqual(recovered_entry['text'], '第二行')
        self.assertEqual(
            [entry['text'] for entry in item['followup_history_seed'][-2:]],
            ['第一行', '第二行'],
        )

    async def test_process_message_rerun_keeps_first_reply_in_history_and_uses_latest_trigger(self):
        first_bundle = {
            'message': '第一次回答',
            'think_note': '',
            'tool_context_messages': [],
            'live_tool_context_checkpointed': True,
            'live_outbound_entries': [
                {
                    'text': '第一次回答',
                    'raw_message': '第一次回答',
                    'message_id': 'r-1',
                    'timestamp': 2.0,
                },
            ],
        }
        second_bundle = {
            'message': '第二次回答',
            'think_note': '',
            'tool_context_messages': [],
            'live_tool_context_checkpointed': True,
            'live_outbound_entries': [
                {
                    'text': '第二次回答',
                    'raw_message': '第二次回答',
                    'message_id': 'r-2',
                    'timestamp': 4.0,
                },
            ],
        }

        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(enabled=True)
        runtime._message_epoch = 1
        runtime._scope_session_modes = {}
        runtime._turn_image_refs = {}
        runtime._event_mailbox = InMemoryEventMailbox()
        runtime._turn_batch_coordinator = AtomicTurnBatchCoordinator(runtime._event_mailbox)
        runtime.repo = Mock()
        runtime.repo.get_or_create_agent = Mock(
            return_value=SimpleNamespace(
                persona='persona',
                impression='',
                display_name='display',
            )
        )
        runtime.repo.get_diary_context = Mock(
            return_value={
                'summaries': [],
                'window': [],
                'pending': [],
                'current': [],
            }
        )
        runtime._message_source_label = lambda _message: 'group'
        runtime._maybe_resolve_display_name = lambda *_args: None
        runtime._flatten_diary_context = lambda _ctx: []
        runtime._build_global_identity_context_for_message = lambda *_args: ''
        runtime._scope_key = lambda *_args: 'group:7'
        runtime._extract_image_refs = lambda _raw: []
        runtime._build_group_context = AsyncMock(return_value='')
        runtime._build_child_messages = Mock(
            side_effect=lambda *_args, **_kwargs: {'system': [], 'messages': []}
        )
        runtime._complete_child_turn = AsyncMock(
            side_effect=[
                (first_bundle, 12, True),
                (second_bundle, 15, False),
            ]
        )
        runtime._drain_live_tool_scope_turn = Mock(
            side_effect=[
                {
                    'message': SimpleNamespace(
                        chat_type='group',
                        chat_id='7',
                        nickname='tester',
                        user_id='42',
                        text='第二个问题',
                        raw_message='@冰糖 第二个问题',
                        message_id='m-2',
                    ),
                    'cleaned': '第二个问题',
                    'trigger_messages': [
                        {'text': '第二个问题', 'raw_message': '@冰糖 第二个问题'}
                    ],
                },
                None,
            ]
        )
        runtime._is_epoch_stale = lambda _epoch: False
        runtime._finalize_reply = lambda _message, reply: reply
        runtime._record_outbound_message = AsyncMock(
            return_value={'text': '第二次回答'}
        )

        first_message = SimpleNamespace(
            chat_type='group',
            chat_id='7',
            nickname='tester',
            user_id='42',
            text='第一个问题',
            raw_message='@冰糖 第一个问题',
            message_id='m-1',
        )
        item = {
            'message_epoch': 1,
            'message': first_message,
            'cleaned': '第一个问题',
            'agent_id': 'agent-1',
            'trigger_messages': [
                {'text': '第一个问题', 'raw_message': '@冰糖 第一个问题'}
            ],
            'deferred_count': 0,
            'scope_key': 'group:7',
        }

        await runtime._process_message(item)

        self.assertEqual(runtime._build_child_messages.call_count, 2)
        second_call = runtime._build_child_messages.call_args_list[1]
        second_history = second_call.args[3]
        second_trigger_messages = second_call.args[5]

        self.assertEqual(
            [entry['text'] for entry in second_trigger_messages],
            ['第二个问题'],
        )
        self.assertEqual(
            [entry['text'] for entry in second_history],
            ['第一个问题', '第一次回答'],
        )
        self.assertEqual(
            [entry['text'] for entry in item['followup_history_seed']],
            ['第一个问题', '第一次回答', '第二个问题', '第二次回答'],
        )


if __name__ == '__main__':
    unittest.main()
