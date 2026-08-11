import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.agent_manager import AgentManager
from core.config import SSHProfileConfig
from core.dev_agent import run_dev_agent
from pack.anthropic_chat_model import AnthropicReply, ToolCall
from pack.json_store import JsonStore


class SequenceModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.model = 'sequence-model'

    def complete(self, system_prompt, messages, tools, *_args):
        self.calls.append({
            'system_prompt': system_prompt,
            'messages': messages,
            'tools': tools,
        })
        return self.replies.pop(0)


class AgentManagerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_create_agent_persists_dispatch_config_and_list_agents_exposes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('config task', cwd='~/core', read_only=True)
            record = manager.get_agent(agent_id)
            listing = manager.list_agents()

        self.assertEqual(record['cwd'], '~/core')
        self.assertTrue(record['read_only'])
        self.assertEqual(listing[0]['agent_id'], agent_id)
        self.assertEqual(listing[0]['cwd'], '~/core')
        self.assertTrue(listing[0]['read_only'])

    def test_agent_error_records_detail_and_list_agents_exposes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('config task')
            manager._set_agent_error(agent_id, '模型调用失败: Connection reset by peer')
            listing = manager.list_agents()

        self.assertEqual(listing[0]['status'], 'error')
        self.assertIn('Connection reset by peer', listing[0]['error_detail'])
        self.assertIsNotNone(listing[0]['last_activity_at'])

    def test_set_status_normal_does_not_write_error_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('config task')
            manager.set_status(agent_id, 'running')
            listing = manager.list_agents()

        self.assertEqual(listing[0]['status'], 'running')
        self.assertEqual(listing[0]['error_detail'], '')

    def test_send_to_agent_can_update_dispatch_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('update config task')

            ok = manager.send_to_agent(
                agent_id,
                {'role': 'user', 'content': '继续'},
                cwd='/test',
                read_only=True,
            )
            record = manager.get_agent(agent_id)

        self.assertTrue(ok)
        self.assertEqual(record['cwd'], '/test')
        self.assertTrue(record['read_only'])

    def test_create_ssh_agent_persists_target_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            manager = AgentManager(store=store)
            agent_id = manager.create_agent(
                'ssh config task',
                cwd='~/app',
                read_only=True,
                target_kind='ssh',
                ssh_profile_id='prod',
            )
            record = manager.get_agent(agent_id)
            listing = manager.list_agents()

        self.assertEqual(record['target_kind'], 'ssh')
        self.assertEqual(record['ssh_profile_id'], 'prod')
        self.assertEqual(listing[0]['target_kind'], 'ssh')
        self.assertEqual(listing[0]['ssh_profile_id'], 'prod')

    async def test_parallel_tool_round_persists_order_and_reaches_idle(self):
        first = AnthropicReply(
            tool_calls=[
                ToolCall('call-a', 'read_local_file', {'path': 'a.py'}),
                ToolCall('call-b', 'search_local_file', {'path': 'b.py', 'query': 'x'}),
                ToolCall('call-c', 'list_local_files', {'subpath': 'core'}),
            ],
            raw_content=[
                {'type': 'tool_use', 'id': 'call-a', 'name': 'read_local_file', 'input': {'path': 'a.py'}},
                {'type': 'tool_use', 'id': 'call-b', 'name': 'search_local_file', 'input': {'path': 'b.py', 'query': 'x'}},
                {'type': 'tool_use', 'id': 'call-c', 'name': 'list_local_files', 'input': {'subpath': 'core'}},
            ],
        )
        second = AnthropicReply(text='完成。\n[[AGENT_DONE]]', raw_content=[{'type': 'text', 'text': '完成。'}])
        model = SequenceModel([first, second])
        emitted = []

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            prompt_path = Path(tmp) / 'agent.txt'
            prompt_path.write_text('test prompt', encoding='utf-8')
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('integration task')

            async def fake_execute(calls, *_args, **_kwargs):
                self.assertEqual([call.call_id for call in calls], ['call-a', 'call-b', 'call-c'])
                return ['result-a', 'result-b', 'result-c']

            async def on_message(aid, text):
                emitted.append((aid, text))

            with patch('core.agent_manager._execute_tool_calls_ordered', side_effect=fake_execute):
                loop_task = asyncio.create_task(
                    manager.run_agent_loop(
                        agent_id,
                        model,
                        '',
                        on_agent_message=on_message,
                        prompt_path=str(prompt_path),
                        project_root=tmp,
                    )
                )
                for _ in range(100):
                    record = manager.get_agent(agent_id) or {}
                    if record.get('status') == 'idle':
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual((manager.get_agent(agent_id) or {}).get('status'), 'idle')
                loop_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await loop_task

            record = manager.get_agent(agent_id)

        self.assertEqual(len(model.calls), 2)
        tool_messages = [
            message for message in record['messages']
            if message.get('role') == 'user' and isinstance(message.get('content'), list)
        ]
        self.assertEqual(len(tool_messages), 1)
        blocks = tool_messages[0]['content']
        self.assertEqual([block['tool_use_id'] for block in blocks], ['call-a', 'call-b', 'call-c'])
        self.assertEqual([block['content'] for block in blocks], ['result-a', 'result-b', 'result-c'])
        self.assertEqual(len(emitted), 1)
        self.assertIn('[[AGENT_DONE]]', emitted[0][1])

    async def test_summary_remains_toolless_and_read_only(self):
        summary_model = SequenceModel([AnthropicReply(text='客观总结')])
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('summary task')
            manager.append_message(agent_id, {'role': 'assistant', 'content': 'progress'})
            before = manager.get_agent(agent_id)
            summary = await manager.summarize_agent(agent_id, model=summary_model)
            after = manager.get_agent(agent_id)

        self.assertEqual(summary, '客观总结')
        self.assertIsNone(summary_model.calls[0]['tools'])
        self.assertEqual(before['messages'], after['messages'])
        self.assertEqual(before['status'], after['status'])

    async def test_review_summary_fallback_is_preserved(self):
        failing_model = SequenceModel([])

        def fail(*_args, **_kwargs):
            raise RuntimeError('summary unavailable')

        failing_model.complete = fail
        with tempfile.TemporaryDirectory() as tmp:
            manager = AgentManager(store=JsonStore(str(Path(tmp) / 'agents.json')))
            agent_id = manager.create_agent('review task')
            review = await manager._build_review_material(agent_id, failing_model, 100)
        self.assertIn('agent 阶段复核请求', review)
        self.assertIn('上下文已保留', review)
        self.assertIn('send_to_agent', review)

    def test_persisted_review_state_resumes_without_losing_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'agents.json')
            first = AgentManager(store=JsonStore(path))
            agent_id = first.create_agent('persistent task')
            first.append_message(agent_id, {'role': 'assistant', 'content': 'saved-progress'})
            first._update_runtime_fields(
                agent_id,
                status='review_required',
                stage_iteration=100,
                review_material='saved-review',
            )

            restored = AgentManager(store=JsonStore(path))
            before_messages = restored.get_agent(agent_id)['messages']
            self.assertTrue(restored.send_to_agent(agent_id, {'role': 'user', 'content': '继续'}))
            record = restored.get_agent(agent_id)

        self.assertEqual(record['status'], 'running')
        self.assertEqual(record['stage_iteration'], 0)
        self.assertEqual(record['messages'], before_messages)
        self.assertEqual(record['review_material'], 'saved-review')

    def test_persisted_error_state_resumes_without_losing_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'agents.json')
            first = AgentManager(store=JsonStore(path))
            agent_id = first.create_agent('persistent error task')
            first.append_message(agent_id, {'role': 'assistant', 'content': 'quota exhausted'})
            first._update_runtime_fields(
                agent_id,
                status='error',
                stage_iteration=7,
            )

            restored = AgentManager(store=JsonStore(path))
            before_messages = restored.get_agent(agent_id)['messages']
            self.assertTrue(restored.send_to_agent(agent_id, {'role': 'user', 'content': '继续'}))
            record = restored.get_agent(agent_id)

        self.assertEqual(record['status'], 'running')
        self.assertEqual(record['stage_iteration'], 0)
        self.assertEqual(record['messages'], before_messages)
        self.assertIn('error_resumed_at', record)

    async def test_destroy_summary_is_generated_before_record_removal(self):
        summary_model = SequenceModel([AnthropicReply(text='销毁前总结')])
        with tempfile.TemporaryDirectory() as tmp:
            manager = AgentManager(store=JsonStore(str(Path(tmp) / 'agents.json')))
            manager.set_model(summary_model)
            agent_id = manager.create_agent('destroy task')
            manager.append_message(agent_id, {'role': 'assistant', 'content': 'changed a file'})
            result = await manager.destroy_agent(agent_id, summarize=True)

        self.assertTrue(result['removed'])
        self.assertEqual(result['summary'], '销毁前总结')
        self.assertIsNone(manager.get_agent(agent_id))
        self.assertIsNone(summary_model.calls[0]['tools'])
        self.assertIn('即将被销毁', summary_model.calls[0]['messages'][0]['content'])

    async def test_waiting_text_without_done_marker_preserves_agent(self):
        model = SequenceModel([
            AnthropicReply(text='需要确认目标文件。', raw_content=[{'type': 'text', 'text': '需要确认目标文件。'}]),
        ])
        emitted = []
        with tempfile.TemporaryDirectory() as tmp:
            manager = AgentManager(store=JsonStore(str(Path(tmp) / 'agents.json')))
            prompt_path = Path(tmp) / 'agent.txt'
            prompt_path.write_text('test prompt', encoding='utf-8')
            agent_id = manager.create_agent('waiting task')

            async def on_message(aid, text):
                emitted.append((aid, text))

            loop_task = asyncio.create_task(manager.run_agent_loop(
                agent_id, model, '', on_agent_message=on_message,
                prompt_path=str(prompt_path), project_root=tmp,
            ))
            for _ in range(100):
                if (manager.get_agent(agent_id) or {}).get('status') == 'waiting':
                    break
                await asyncio.sleep(0.01)
            record = manager.get_agent(agent_id)
            loop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop_task

        self.assertIsNotNone(record)
        self.assertEqual(record['status'], 'waiting')
        self.assertIn('需要确认目标文件', record['messages'][-1]['content'])
        self.assertEqual(len(emitted), 1)

    async def test_run_agent_loop_passes_dispatch_config_to_executor(self):
        model = SequenceModel([
            AnthropicReply(
                tool_calls=[ToolCall('call-a', 'read_local_file', {'path': 'core/a.py'})],
                raw_content=[
                    {'type': 'tool_use', 'id': 'call-a', 'name': 'read_local_file', 'input': {'path': 'core/a.py'}},
                ],
            ),
            AnthropicReply(text='完成。\n[[AGENT_DONE]]', raw_content=[{'type': 'text', 'text': '完成。'}]),
        ])
        observed = {}

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            prompt_path = Path(tmp) / 'agent.txt'
            prompt_path.write_text('test prompt', encoding='utf-8')
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('config execute task', cwd='~/core', read_only=True)

            async def fake_execute(calls, *_args, **kwargs):
                observed['default_cwd'] = kwargs.get('default_cwd')
                observed['read_only'] = kwargs.get('read_only')
                return ['ok']

            with patch('core.agent_manager._execute_tool_calls_ordered', side_effect=fake_execute):
                loop_task = asyncio.create_task(manager.run_agent_loop(
                    agent_id, model, '', on_agent_message=manager.on_agent_message,
                    prompt_path=str(prompt_path), project_root=tmp,
                ))
                for _ in range(100):
                    if (manager.get_agent(agent_id) or {}).get('status') == 'idle':
                        break
                    await asyncio.sleep(0.01)
                loop_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await loop_task

        self.assertEqual(observed['default_cwd'], '~/core')
        self.assertTrue(observed['read_only'])

    async def test_run_ssh_agent_loop_passes_ssh_profile_and_prompt_context(self):
        model = SequenceModel([
            AnthropicReply(
                tool_calls=[ToolCall('call-a', 'read_local_file', {'path': 'core/a.py'})],
                raw_content=[
                    {'type': 'tool_use', 'id': 'call-a', 'name': 'read_local_file', 'input': {'path': 'core/a.py'}},
                ],
            ),
            AnthropicReply(text='完成。\n[[AGENT_DONE]]', raw_content=[{'type': 'text', 'text': '完成。'}]),
        ])
        observed = {}
        profile = SSHProfileConfig(
            profile_id='prod',
            target='root@example.com',
            root_dir='/srv/app',
            port=22,
            identity_file='',
            shell='bash',
            strict_host_key_checking=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            prompt_path = Path(tmp) / 'agent.txt'
            prompt_path.write_text('test prompt', encoding='utf-8')
            manager = AgentManager(store=store)
            agent_id = manager.create_agent(
                'ssh execute task',
                target_kind='ssh',
                ssh_profile_id='prod',
            )

            async def fake_execute(calls, *_args, **kwargs):
                observed['ssh_profile'] = kwargs.get('ssh_profile')
                return ['ok']

            with patch('core.agent_manager._execute_tool_calls_ordered', side_effect=fake_execute):
                loop_task = asyncio.create_task(manager.run_agent_loop(
                    agent_id, model, '', on_agent_message=manager.on_agent_message,
                    prompt_path=str(prompt_path), project_root=tmp,
                    ssh_profiles={'prod': profile},
                ))
                for _ in range(100):
                    if (manager.get_agent(agent_id) or {}).get('status') == 'idle':
                        break
                    await asyncio.sleep(0.01)
                loop_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await loop_task

        self.assertIs(observed['ssh_profile'], profile)
        self.assertIn('远程 SSH 环境', model.calls[0]['system_prompt'])
        self.assertIn('root@example.com', model.calls[0]['system_prompt'])
        tool_names = {tool.get('name') for tool in (model.calls[0]['tools'] or [])}
        self.assertIn('ssh_download_file', tool_names)
        self.assertIn('ssh_upload_file', tool_names)
        self.assertIn('ssh_transfer_status', tool_names)

    async def test_tool_execution_failure_marks_error_and_queues_urgent_report(self):
        model = SequenceModel([
            AnthropicReply(
                tool_calls=[ToolCall('call-a', 'read_local_file', {'path': 'a.py'})],
                raw_content=[
                    {'type': 'tool_use', 'id': 'call-a', 'name': 'read_local_file', 'input': {'path': 'a.py'}},
                ],
            ),
        ])

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            prompt_path = Path(tmp) / 'agent.txt'
            prompt_path.write_text('test prompt', encoding='utf-8')
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('failing tool task', origin_scope='group:7')

            async def fail_execute(*_args, **_kwargs):
                raise RuntimeError('tool exploded')

            with patch('core.agent_manager._execute_tool_calls_ordered', side_effect=fail_execute):
                await manager.run_agent_loop(
                    agent_id,
                    model,
                    '',
                    on_agent_message=manager.on_agent_message,
                    prompt_path=str(prompt_path),
                    project_root=tmp,
                )

            record = manager.get_agent(agent_id)
            reports = manager.drain_pending_reports()

        self.assertEqual(record['status'], 'error')
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].get('urgent'))
        self.assertEqual(reports[0].get('origin_scope'), 'group:7')
        self.assertIn('常驻循环异常退出', reports[0].get('text') or '')
        self.assertIn('tool exploded', reports[0].get('text') or '')

    async def test_terminal_failure_with_done_marker_is_not_treated_as_idle(self):
        model = SequenceModel([
            AnthropicReply(
                text='失败：目标文件不存在，无法继续\n[[AGENT_DONE]]',
                raw_content=[{'type': 'text', 'text': '失败：目标文件不存在，无法继续\n[[AGENT_DONE]]'}],
            ),
        ])

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            prompt_path = Path(tmp) / 'agent.txt'
            prompt_path.write_text('test prompt', encoding='utf-8')
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('done marker failure task', origin_scope='group:7')

            await manager.run_agent_loop(
                agent_id,
                model,
                '',
                on_agent_message=manager.on_agent_message,
                prompt_path=str(prompt_path),
                project_root=tmp,
            )

            record = manager.get_agent(agent_id)
            reports = manager.drain_pending_reports()

        self.assertEqual(record['status'], 'error')
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].get('urgent'))
        self.assertIn('失败：目标文件不存在', reports[0].get('text') or '')

    async def test_model_consecutive_failures_marks_error_and_queues_urgent_report(self):
        class FailingModel:
            model = 'failing-model'

            def complete(self, system_prompt, messages, tools, *_args):
                raise TimeoutError('gateway timeout after retries')

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            prompt_path = Path(tmp) / 'agent.txt'
            prompt_path.write_text('test prompt', encoding='utf-8')
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('model fail task', origin_scope='group:7')

            with patch('core.agent_manager.asyncio.sleep', new=AsyncMock()), \
                    patch('core.agent_manager.time.sleep'), \
                    patch('core.agent_manager.AgentManager._run_blocking', new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))):
                await manager.run_agent_loop(
                    agent_id,
                    FailingModel(),
                    '',
                    on_agent_message=manager.on_agent_message,
                    prompt_path=str(prompt_path),
                    project_root=tmp,
                )

            record = manager.get_agent(agent_id)
            reports = manager.drain_pending_reports()

        self.assertEqual(record['status'], 'error')
        self.assertIn('模型连续 3 次调用失败', record['last_error'])
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].get('urgent'))
        self.assertEqual(reports[0].get('origin_scope'), 'group:7')
        self.assertIn('模型连续 3 次调用失败', reports[0].get('text') or '')

    async def test_model_non_retryable_error_marks_error_with_detail_and_queues_urgent_report(self):
        class FailingModel:
            model = 'failing-model'

            def complete(self, system_prompt, messages, tools, *_args):
                raise RuntimeError('schema validation failed')

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(str(Path(tmp) / 'agents.json'))
            prompt_path = Path(tmp) / 'agent.txt'
            prompt_path.write_text('test prompt', encoding='utf-8')
            manager = AgentManager(store=store)
            agent_id = manager.create_agent('model hard fail task', origin_scope='group:9')

            with patch('core.agent_manager.asyncio.sleep', new=AsyncMock()), \
                    patch('core.agent_manager.time.sleep'), \
                    patch('core.agent_manager.AgentManager._run_blocking', new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))):
                await manager.run_agent_loop(
                    agent_id,
                    FailingModel(),
                    '',
                    on_agent_message=manager.on_agent_message,
                    prompt_path=str(prompt_path),
                    project_root=tmp,
                )

            record = manager.get_agent(agent_id)
            reports = manager.drain_pending_reports()

        self.assertEqual(record['status'], 'error')
        self.assertIn('schema validation failed', record['last_error'])
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].get('urgent'))
        self.assertEqual(reports[0].get('origin_scope'), 'group:9')
        self.assertIn('schema validation failed', reports[0].get('text') or '')




class TaskerCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_tasker_uses_ordered_executor_and_keeps_finish_callback(self):
        first = AnthropicReply(
            tool_calls=[
                ToolCall('task-a', 'read_local_file', {'path': 'a.py'}),
                ToolCall('task-b', 'search_local_file', {'path': 'b.py', 'query': 'x'}),
            ],
            raw_content=[
                {'type': 'tool_use', 'id': 'task-a', 'name': 'read_local_file', 'input': {'path': 'a.py'}},
                {'type': 'tool_use', 'id': 'task-b', 'name': 'search_local_file', 'input': {'path': 'b.py', 'query': 'x'}},
            ],
        )
        model = SequenceModel([first, AnthropicReply(text='tasker done')])
        finished = []

        async def fake_execute(calls, *_args, **_kwargs):
            self.assertEqual([call.call_id for call in calls], ['task-a', 'task-b'])
            return ['task-result-a', 'task-result-b']

        async def on_finished(payload):
            finished.append(payload)

        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / 'tasker.txt'
            prompt_path.write_text('tasker prompt', encoding='utf-8')
            with patch('core.dev_agent._execute_tool_calls_ordered', side_effect=fake_execute):
                result = await run_dev_agent(
                    model, '', 'tasker task', prompt_path=str(prompt_path),
                    project_root=tmp, on_finished=on_finished,
                )

        self.assertEqual(result, 'tasker done')
        self.assertEqual(len(model.calls), 2)
        blocks = model.calls[1]['messages'][-1]['content']
        self.assertEqual([block['tool_use_id'] for block in blocks], ['task-a', 'task-b'])
        self.assertEqual([block['content'] for block in blocks], ['task-result-a', 'task-result-b'])
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]['status'], 'done')
        self.assertEqual(finished[0]['result'], 'tasker done')
if __name__ == '__main__':
    unittest.main()
