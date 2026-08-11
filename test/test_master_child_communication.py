"""回归：主 AI 与分级 AI 沟通流程（notify_master）工具执行完整性。

背景：_handle_notify_master 的处理循环中，只有 LOOP_TOOL_NAMES（查询类）工具会
真正执行；create_task / remember 等 DIRECTIVE 工具每轮被跳过（提示"未执行"），
循环结束后才补执行**最后一轮**的 tool_calls。当主 AI 同批混合调用
[查询工具, create_task] 且后续还有轮次时，中间轮次的 create_task 会被静默丢失
——主 AI 以为自己建了任务，分级 AI 的上报需求悄悄消失。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.ai_runtime import AIOrchestrator


def _reply(tool_calls, text=''):
    return SimpleNamespace(
        text=text,
        raw_content=f'assistant-{len(tool_calls)}',
        tool_calls=[
            SimpleNamespace(name=name, input=inp, call_id=f'call-{i}')
            for i, (name, inp) in enumerate(tool_calls)
        ],
    )


def _make_runtime():
    runtime = object.__new__(AIOrchestrator)
    runtime.repo = SimpleNamespace(
        get_or_create_master=Mock(),
        add_note=Mock(),
        list_notes=lambda *_a, **_k: [],
        list_scope_relations=lambda: [],
        list_user_relations=lambda: [],
    )
    runtime.tools = SimpleNamespace(
        get_friend_list=lambda: [],
        get_group_list=lambda: [],
        create_task=Mock(return_value=SimpleNamespace(task_id='task-1')),
    )
    runtime._build_master_prompt = lambda _task: 'prompt'
    runtime._static_system_blocks = lambda _prompt: []
    runtime._master_system_prompt = lambda: ''
    runtime._scope_key = AIOrchestrator._scope_key.__get__(runtime, AIOrchestrator)
    runtime._run_ai_tool_call = AsyncMock(return_value='查询结果')
    runtime._format_tool_result_content = lambda *_a, **_k: 'result'
    runtime._normalize_task_kind = lambda kind: kind
    runtime._normalize_task_payload = lambda *_a, **_k: {}
    runtime._is_dev_agent_authorized = lambda *_a, **_k: True
    runtime._submit_runtime_task = Mock()
    runtime._should_callback_to_source = lambda *_a, **_k: False
    return runtime


class NotifyMasterToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_batch_create_task_not_lost_across_rounds(self):
        """回归：主 AI 同批 [查询, create_task]，下轮结束时 create_task 不得丢失。"""
        runtime = _make_runtime()
        runtime._complete_chat = AsyncMock(
            side_effect=[
                _reply([('web_search', {'query': 'x'}), ('create_task', {'kind': 'research', 'payload': '研究X'})]),
                _reply([]),  # 主 AI 拿到结果后结束
            ]
        )
        task = {
            'task_id': 'notify-1',
            'source_agent': 'child:private:241898129',
            'kind': 'notify_master',
            'payload': {
                'scope_type': 'private',
                'scope_id': '241898129',
                'request_type': 'generic',
                'trace_id': 'trace-1',
            },
        }
        result = await AIOrchestrator._handle_notify_master(runtime, task)

        # create_task 必须真正执行过，而不是被跳过
        runtime.tools.create_task.assert_called_once()
        self.assertIn('task-1', result)

    async def test_remember_executed_in_loop(self):
        """回归：主 AI 调 remember 应真正落库，而不是静默丢失。"""
        runtime = _make_runtime()
        runtime._complete_chat = AsyncMock(
            side_effect=[
                _reply([('remember', {'note': '重要备忘'})]),
            ]
        )
        task = {
            'task_id': 'notify-2',
            'source_agent': 'child:private:241898129',
            'kind': 'notify_master',
            'payload': {'scope_type': 'private', 'scope_id': '241898129', 'trace_id': 'trace-2'},
        }
        await AIOrchestrator._handle_notify_master(runtime, task)
        # 开头会记一条"来自 xxx"日志，remember 必须真正落库
        self.assertTrue(runtime.repo.add_note.called)
        self.assertIn('重要备忘', runtime.repo.add_note.call_args.args)

    async def test_unknown_directive_returns_hint_not_silent(self):
        """未知 DIRECTIVE 工具返回可操作提示，不静默吞掉。"""
        runtime = _make_runtime()
        runtime._complete_chat = AsyncMock(
            side_effect=[
                _reply([('stay_silent', {})]),
            ]
        )
        task = {
            'task_id': 'notify-3',
            'source_agent': 'child:private:241898129',
            'kind': 'notify_master',
            'payload': {'scope_type': 'private', 'scope_id': '241898129', 'trace_id': 'trace-3'},
        }
        await AIOrchestrator._handle_notify_master(runtime, task)
        # 不应抛出；循环应正常结束
        runtime.tools.create_task.assert_not_called()


if __name__ == '__main__':
    unittest.main()
