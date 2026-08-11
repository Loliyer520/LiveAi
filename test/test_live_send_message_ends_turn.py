import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.ai_runtime import AIOrchestrator


class LiveSendMessageEndsTurnTests(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(self, *, is_openai_protocol=False):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(admin_qq=1, history_limit=20)
        runtime.model = SimpleNamespace(is_openai_protocol=is_openai_protocol)
        runtime.tools = Mock()
        runtime.tools.record_tool_use = Mock()
        runtime._scope_thinking_levels = {}
        runtime._scope_session_modes = {}
        runtime._pending_self_interrupts = {}
        runtime._pending_send_message_persona_notices = {}
        runtime._character_sessions = SimpleNamespace(
            pop_tool_raw=lambda _scope_key: None
        )
        runtime._scope_key = lambda scope_type, scope_id: f'{scope_type}:{scope_id}'
        runtime._is_epoch_stale = lambda _epoch: False
        runtime._filter_thinking_blocks = lambda raw: raw
        runtime._normalize_think_note = lambda text: text or ''
        runtime._append_live_tool_checkpoint = Mock(
            return_value={'text': '', 'tool_context_messages': []}
        )
        runtime._drain_live_tool_scope_turn = Mock(return_value=None)
        runtime._build_pending_fold_reminder = Mock(return_value='pending')
        runtime._record_turn_log = AsyncMock()
        return runtime

    async def test_send_message_finishes_live_turn_without_second_provider_call(self):
        """纯 send_message（未传 continue_work）保持旧语义：发送即终结，无多余轮次。"""
        runtime = self._make_runtime()

        def fake_live_tool_call(
            _scope_type,
            _scope_id,
            _agent_id,
            _message,
            _call,
            _context,
            _allow_notify_master,
            _allow_tasks,
            sent_entries,
            live_outbound_entries,
            live_send_action_ledger=None,
        ):
            sent_entries.append({'text': '第一行', 'message_id': '1'})
            sent_entries.append({'text': '第二行', 'message_id': '2'})
            live_outbound_entries.append({'text': '第一行', 'message_id': '1'})
            live_outbound_entries.append({'text': '第二行', 'message_id': '2'})
            return '已发送 2 条消息。'

        runtime._execute_live_action_tool_call = fake_live_tool_call
        runtime._complete_chat = AsyncMock(
            return_value=SimpleNamespace(
                text='',
                tool_calls=[
                    SimpleNamespace(
                        name='send_message',
                        input={'content': '第一行\n第二行'},
                        call_id='call-1',
                    )
                ],
                raw_content='tool use',
                stop_reason='tool_use',
            )
        )

        live_message = SimpleNamespace(chat_type='private', chat_id='7')
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=live_message,
        )

        self.assertEqual(runtime._complete_chat.await_count, 1)
        self.assertEqual(bundle['message'], '第一行\n第二行')
        self.assertEqual(len(bundle['live_outbound_entries']), 2)
        runtime._record_turn_log.assert_awaited_once()

    async def test_send_message_drains_pending_before_turn_returns(self):
        runtime = self._make_runtime()
        runtime._drain_live_tool_scope_turn.return_value = {
            'message': SimpleNamespace(chat_type='private', chat_id='7'),
            'cleaned': '后续上报',
            'trigger_messages': [{'text': '后续上报'}],
            'deferred_count': 1,
        }

        def fake_live_tool_call(
            _scope_type,
            _scope_id,
            _agent_id,
            _message,
            _call,
            _context,
            _allow_notify_master,
            _allow_tasks,
            sent_entries,
            live_outbound_entries,
            live_send_action_ledger=None,
        ):
            sent_entries.append({'text': '第一行', 'message_id': '1'})
            live_outbound_entries.append({'text': '第一行', 'message_id': '1'})
            return '已发送 1 条消息。'

        runtime._execute_live_action_tool_call = fake_live_tool_call
        runtime._complete_chat = AsyncMock(
            return_value=SimpleNamespace(
                text='',
                tool_calls=[
                    SimpleNamespace(
                        name='send_message',
                        input={'content': '第一行'},
                        call_id='call-1',
                    )
                ],
                raw_content='tool use',
                stop_reason='tool_use',
            )
        )

        live_message = SimpleNamespace(chat_type='private', chat_id='7')
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=live_message,
        )

        runtime._drain_live_tool_scope_turn.assert_called_once_with('private:7')
        self.assertIn('post_send_pending', bundle)
        self.assertEqual(bundle['post_send_pending']['cleaned'], '后续上报')
        runtime._record_turn_log.assert_awaited_once()

    async def test_send_message_result_is_wrapped_user_visible_when_mixed_with_other_tools(self):
        runtime = self._make_runtime()
        runtime._run_ai_tool_call = AsyncMock(return_value='查询结果')

        def fake_live_tool_call(
            _scope_type,
            _scope_id,
            _agent_id,
            _message,
            _call,
            _context,
            _allow_notify_master,
            _allow_tasks,
            sent_entries,
            live_outbound_entries,
            live_send_action_ledger=None,
        ):
            sent_entries.append({'text': '最终回复', 'message_id': '1'})
            live_outbound_entries.append({'text': '最终回复', 'message_id': '1'})
            return '已发送 1 条消息。发送内容：\n最终回复'

        runtime._execute_live_action_tool_call = fake_live_tool_call
        # 修复后混合批次不因 send_message 终结：第二轮模型消费查询结果后决定结束（无新工具调用）
        runtime._complete_chat = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='memory_list',
                            input={},
                            call_id='call-1',
                        ),
                        SimpleNamespace(
                            name='send_message',
                            input={'content': '最终回复'},
                            call_id='call-2',
                        ),
                    ],
                    raw_content='tool use',
                    stop_reason='tool_use',
                ),
                SimpleNamespace(
                    text='',
                    tool_calls=[],
                    raw_content='',
                    stop_reason='end_turn',
                ),
            ]
        )

        live_message = SimpleNamespace(chat_type='private', chat_id='7')
        await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=live_message,
        )

        # 混合批次发送后回合不终结，模型获得第二轮消费查询结果
        self.assertEqual(runtime._complete_chat.await_count, 2)
        logged_messages = runtime._record_turn_log.await_args.args[3]
        mixed_result_blocks = logged_messages[-1]['content']
        self.assertEqual(mixed_result_blocks[0]['content'], '查询结果')
        self.assertEqual(
            mixed_result_blocks[1]['content'],
            '<user_visible>已发送 1 条消息。发送内容：\n最终回复</user_visible>',
        )

    async def test_mixed_batch_confirm_message_then_continue_working(self):
        """回归：'好，我马上去' + 查询工具同批后不得沉默。

        此前 send_message 一旦发送就终结回合，同批查询工具的结果被丢弃，
        模型永远没有下一轮消费结果继续工作，表现为发完确认就沉默。
        """
        runtime = self._make_runtime()
        runtime._run_ai_tool_call = AsyncMock(return_value='文件内容: 找到了答案')

        def fake_live_tool_call(
            _scope_type,
            _scope_id,
            _agent_id,
            _message,
            _call,
            _context,
            _allow_notify_master,
            _allow_tasks,
            sent_entries,
            live_outbound_entries,
            live_send_action_ledger=None,
        ):
            content = str(_call.input.get('content') or '')
            if '确认' in content:
                sent_entries.append({'text': '好，我马上去', 'message_id': 'm1'})
                live_outbound_entries.append({'text': '好，我马上去', 'message_id': 'm1'})
                return '已发送 1 条消息。发送内容：\n好，我马上去'
            sent_entries.append({'text': '已完成：找到了答案', 'message_id': 'm2'})
            live_outbound_entries.append({'text': '已完成：找到了答案', 'message_id': 'm2'})
            return '已发送 1 条消息。发送内容：\n已完成：找到了答案'

        runtime._execute_live_action_tool_call = fake_live_tool_call
        runtime._complete_chat = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='send_message',
                            input={'content': '确认'},
                            call_id='call-1',
                        ),
                        SimpleNamespace(
                            name='web_search',
                            input={'query': '答案在哪'},
                            call_id='call-2',
                        ),
                    ],
                    raw_content='tool use',
                    stop_reason='tool_use',
                ),
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='send_message',
                            input={'content': '已完成'},
                            call_id='call-3',
                        ),
                    ],
                    raw_content='tool use',
                    stop_reason='tool_use',
                ),
            ]
        )

        live_message = SimpleNamespace(chat_type='private', chat_id='7')
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=live_message,
        )

        # 两轮生成：确认消息 → 消费查询结果后发送最终结果并结束
        self.assertEqual(runtime._complete_chat.await_count, 2)
        self.assertIn('好，我马上去', bundle['message'])
        self.assertIn('已完成：找到了答案', bundle['message'])
        self.assertEqual(len(bundle['live_outbound_entries']), 2)

    async def test_send_message_then_continue_work_in_next_round(self):
        """回归（续期/续跑链路）：send_message 传 continue_work=true 后继续干活。

        模型先发确认消息并显式声明 continue_work=true，本回合保留后续轮次，
        下一轮可继续调用 create_task 完成续期操作，不再“发完确认就静默”。
        """
        runtime = self._make_runtime()
        created_tasks = []

        def fake_live_tool_call(
            _scope_type,
            _scope_id,
            _agent_id,
            _message,
            _call,
            _context,
            _allow_notify_master,
            _allow_tasks,
            sent_entries,
            live_outbound_entries,
            live_send_action_ledger=None,
        ):
            if _call.name == 'send_message':
                sent_entries.append({'text': '好的，我马上去续期', 'message_id': 'm1'})
                live_outbound_entries.append({'text': '好的，我马上去续期', 'message_id': 'm1'})
                return '已发送 1 条消息。发送内容：\n好的，我马上去续期'
            if _call.name == 'create_task':
                created_tasks.append(dict(_call.input))
                return '已创建任务 8888。'
            return 'ok'

        runtime._execute_live_action_tool_call = fake_live_tool_call
        runtime._complete_chat = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='send_message',
                            input={'content': '好的，我马上去续期', 'continue_work': True},
                            call_id='call-1',
                        )
                    ],
                    raw_content='tool use',
                    stop_reason='tool_use',
                ),
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='create_task',
                            input={'kind': 'generic', 'payload': '续期服务器'},
                            call_id='call-2',
                        )
                    ],
                    raw_content='tool use',
                    stop_reason='tool_use',
                ),
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='stay_silent',
                            input={},
                            call_id='call-3',
                        )
                    ],
                    raw_content='stay silent',
                    stop_reason='tool_use',
                ),
            ]
        )

        live_message = SimpleNamespace(chat_type='private', chat_id='7')
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=live_message,
        )

        # continue_work=true 使发送后回合不终结，模型获得后续轮次完成续期操作
        self.assertEqual(runtime._complete_chat.await_count, 3)
        self.assertEqual(created_tasks, [{'kind': 'generic', 'payload': '续期服务器'}])
        self.assertIn('好的，我马上去续期', bundle['message'])

    async def test_send_message_continue_work_false_finishes_live_turn(self):
        """send_message 显式传 continue_work=false 与缺省一致：发送即终结，无多余轮次。"""
        runtime = self._make_runtime()

        def fake_live_tool_call(
            _scope_type,
            _scope_id,
            _agent_id,
            _message,
            _call,
            _context,
            _allow_notify_master,
            _allow_tasks,
            sent_entries,
            live_outbound_entries,
            live_send_action_ledger=None,
        ):
            sent_entries.append({'text': '最终答复', 'message_id': 'm1'})
            live_outbound_entries.append({'text': '最终答复', 'message_id': 'm1'})
            return '已发送 1 条消息。发送内容：\n最终答复'

        runtime._execute_live_action_tool_call = fake_live_tool_call
        runtime._complete_chat = AsyncMock(
            return_value=SimpleNamespace(
                text='',
                tool_calls=[
                    SimpleNamespace(
                        name='send_message',
                        input={'content': '最终答复', 'continue_work': False},
                        call_id='call-1',
                    )
                ],
                raw_content='tool use',
                stop_reason='tool_use',
            )
        )

        live_message = SimpleNamespace(chat_type='private', chat_id='7')
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=live_message,
        )

        self.assertEqual(runtime._complete_chat.await_count, 1)
        self.assertEqual(bundle['message'], '最终答复')
        runtime._record_turn_log.assert_awaited_once()

    async def test_openai_loop_guard_stay_silent_keeps_live_outbound_entries_contract(self):
        runtime = self._make_runtime(is_openai_protocol=True)
        runtime._execute_live_action_tool_call = Mock()
        runtime._run_ai_tool_call = AsyncMock(return_value='查询结果')
        runtime._complete_chat = AsyncMock(
            side_effect=[
                *[
                    SimpleNamespace(
                        text='',
                        tool_calls=[
                            SimpleNamespace(
                                name='memory_list',
                                input={},
                                call_id=f'loop-{index}',
                            )
                        ],
                        raw_content='tool use',
                        stop_reason='tool_use',
                    )
                    for index in range(8)
                ],
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='stay_silent',
                            input={},
                            call_id='final-stay',
                        )
                    ],
                    raw_content='stay silent',
                    stop_reason='tool_use',
                ),
            ]
        )

        live_message = SimpleNamespace(chat_type='private', chat_id='7')
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=live_message,
        )

        self.assertIn('live_outbound_entries', bundle)
        self.assertEqual(bundle['live_outbound_entries'], [])
        runtime._record_turn_log.assert_awaited_once()

    async def test_loop_guard_terminal_bundle_keeps_live_outbound_entries_contract(self):
        runtime = self._make_runtime()
        runtime._execute_live_action_tool_call = Mock()
        runtime._run_ai_tool_call = AsyncMock(return_value='查询结果')
        runtime._complete_chat = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='memory_list',
                            input={},
                            call_id=f'loop-{index}',
                        )
                    ],
                    raw_content='tool use',
                    stop_reason='tool_use',
                )
                for index in range(8)
            ]
        )

        live_message = SimpleNamespace(chat_type='private', chat_id='7')
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=live_message,
        )

        self.assertEqual(bundle['turn_metadata']['note'], 'tool_loop_guard')
        self.assertIn('live_outbound_entries', bundle)
        self.assertEqual(bundle['live_outbound_entries'], [])
        runtime.tools.record_tool_use.assert_called_once()
        runtime._record_turn_log.assert_awaited_once()

    async def test_openai_loop_guard_plain_text_bundle_keeps_live_outbound_entries_contract(self):
        runtime = self._make_runtime(is_openai_protocol=True)
        runtime._execute_live_action_tool_call = Mock()
        runtime._run_ai_tool_call = AsyncMock(return_value='查询结果')
        runtime._complete_chat = AsyncMock(
            side_effect=[
                *[
                    SimpleNamespace(
                        text='',
                        tool_calls=[
                            SimpleNamespace(
                                name='memory_list',
                                input={},
                                call_id=f'loop-{index}',
                            )
                        ],
                        raw_content='tool use',
                        stop_reason='tool_use',
                    )
                    for index in range(8)
                ],
                SimpleNamespace(
                    text='最终普通文字',
                    tool_calls=[],
                    raw_content='最终普通文字',
                    stop_reason='end_turn',
                ),
            ]
        )

        live_message = SimpleNamespace(chat_type='private', chat_id='7')
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=live_message,
        )

        self.assertEqual(bundle['message'], '')
        self.assertIn('live_outbound_entries', bundle)
        self.assertEqual(bundle['live_outbound_entries'], [])
        self.assertEqual(bundle['turn_metadata']['note'], 'tool_loop_guard_plaintext_blocked')
        runtime._record_turn_log.assert_awaited_once()

    async def test_next_turn_persona_notice_is_transient_only(self):
        runtime = self._make_runtime()
        runtime._run_ai_tool_call = AsyncMock(return_value='查询结果')
        AIOrchestrator._mark_send_message_persona_notice(runtime, 'private', '7', 'agent-1')

        seen_messages = []

        async def fake_complete_chat(_system_blocks, messages, _round_tools, _temperature, scope_key=None, role=None):
            seen_messages.append({'scope_key': scope_key, 'messages': deepcopy(messages)})
            if len(seen_messages) == 1:
                return SimpleNamespace(
                    text='',
                    tool_calls=[SimpleNamespace(name='memory_list', input={}, call_id='call-1')],
                    raw_content='tool use',
                    stop_reason='tool_use',
                )
            return SimpleNamespace(
                text='收到',
                tool_calls=[],
                raw_content='收到',
                stop_reason='end_turn',
            )

        runtime._complete_chat = fake_complete_chat

        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'message'},
            live_message=None,
        )

        self.assertEqual(bundle['message'], '')
        self.assertEqual(bundle['think_note'], '收到')
        self.assertEqual(len(seen_messages), 2)
        self.assertEqual(
            seen_messages[1]['messages'][-1]['content'][0]['content'],
            '查询结果\n<notice>请回归人设发言！</notice>',
        )
        logged_messages = runtime._record_turn_log.await_args.args[3]
        self.assertEqual(logged_messages[-1]['content'][0]['content'], '查询结果')
        self.assertFalse(runtime._pending_send_message_persona_notices)

    def test_directive_send_message_over_twenty_chars_sets_next_turn_notice(self):
        runtime = self._make_runtime()

        result = AIOrchestrator._apply_directive_tools(
            runtime,
            'private',
            '7',
            'agent-1',
            [
                SimpleNamespace(
                    name='send_message',
                    input={'content': '这是一条超过二十个字的人设提醒触发消息内容'},
                )
            ],
        )

        self.assertEqual(result, '这是一条超过二十个字的人设提醒触发消息内容')
        self.assertTrue(
            AIOrchestrator._consume_send_message_persona_notice(runtime, 'private', '7', 'agent-1')
        )

    def test_directive_send_message_strips_thinking_from_aggregated_reply(self):
        runtime = self._make_runtime()
        # 纯思维链：剥完后为空 → 模型沉默，不发任何内容
        silent = AIOrchestrator._apply_directive_tools(
            runtime,
            'private',
            '7',
            'agent-1',
            [
                SimpleNamespace(
                    name='send_message',
                    input={'content': '<thinking>本轮为补充情报工单，无新增可沉淀内容，选择保持沉默。</thinking>'},
                )
            ],
        )
        self.assertEqual(silent, '')
        # 思维链 + 正文：只保留标签外的正文
        mixed = AIOrchestrator._apply_directive_tools(
            runtime,
            'private',
            '7',
            'agent-1',
            [
                SimpleNamespace(
                    name='send_message',
                    input={'content': '<thinking>先想一下</thinking>好的，已经处理完成。'},
                )
            ],
        )
        self.assertEqual(mixed, '好的，已经处理完成。')

    async def test_background_mixed_batch_keeps_send_message_confirm(self):
        """回归：后台链路（live_message=None）混合批次 '好，我马上去' + 查询不得丢失。

        此前混合批次里 send_message 只返回占位提示，模型下轮若不回调
        （stay_silent/普通文本），确认消息凭空消失，表现为链路中途截止。
        """
        runtime = self._make_runtime()
        runtime._run_ai_tool_call = AsyncMock(return_value='查询结果：找到了')

        runtime._complete_chat = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='send_message',
                            input={'content': '好，我马上去'},
                            call_id='call-1',
                        ),
                        SimpleNamespace(
                            name='web_search',
                            input={'query': '答案在哪'},
                            call_id='call-2',
                        ),
                    ],
                    raw_content='tool use',
                    stop_reason='tool_use',
                ),
                # 模型下轮不再回调 send_message（以为已确认），只输出普通文本
                SimpleNamespace(
                    text='正在处理',
                    tool_calls=[],
                    raw_content='正在处理',
                    stop_reason='end_turn',
                ),
            ]
        )

        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'delegate'},
            live_message=None,
        )

        self.assertEqual(runtime._complete_chat.await_count, 2)
        # 第一轮的确认消息必须保留在最终回报里，不再被占位提示吞掉
        self.assertEqual(bundle['message'], '好，我马上去')

    async def test_background_mixed_batch_merges_final_reply(self):
        """后台链路混合批次后模型再发最终回复：确认 + 结果合并为一条回报。"""
        runtime = self._make_runtime()
        runtime._run_ai_tool_call = AsyncMock(return_value='文件内容: 找到了答案')

        runtime._complete_chat = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='send_message',
                            input={'content': '好，我马上去'},
                            call_id='call-1',
                        ),
                        SimpleNamespace(
                            name='memory_list',
                            input={},
                            call_id='call-2',
                        ),
                    ],
                    raw_content='tool use',
                    stop_reason='tool_use',
                ),
                SimpleNamespace(
                    text='',
                    tool_calls=[
                        SimpleNamespace(
                            name='send_message',
                            input={'content': '已完成：找到了答案'},
                            call_id='call-3',
                        ),
                    ],
                    raw_content='tool use',
                    stop_reason='tool_use',
                ),
            ]
        )

        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'private',
            '7',
            'agent-1',
            {'system': [], 'messages': []},
            0.85,
            run_epoch=1,
            turn_meta={'turn_kind': 'delegate'},
            live_message=None,
        )

        self.assertEqual(runtime._complete_chat.await_count, 2)
        self.assertEqual(bundle['message'], '好，我马上去\n已完成：找到了答案')

    def test_turn_result_bundle_strips_thinking_from_message(self):
        runtime = self._make_runtime()
        bundle = AIOrchestrator._turn_result_bundle(
            runtime,
            {'message': '<thinking>内部思考</thinking>给用户的正文', 'think_note': 'x'},
            turn_log_committed=True,
            agent_id='agent-1',
            temperature=0.85,
            turn_meta={},
            tool_iterations=[],
            generation_ms=1,
        )
        self.assertEqual(bundle['message'], '给用户的正文')


if __name__ == '__main__':
    unittest.main()
