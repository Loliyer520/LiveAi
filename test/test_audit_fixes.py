"""审计轮修复回归测试（2026-08-05）。

覆盖：
1. roundrobin 只读展示不推进轮询计数（advance=False）；
2. recurring 无效 cron / 无效 target_scope 防抖停用（不再每 30s 重复触发）；
3. task ingress router 异常任务落失败状态（不静默丢失）；
4. switch_channel_model 切换后同步渠道轮询/回退索引（main 不失同步）；
5. diary 触发消息按 message_id 精确剔除（不按条数切尾误删/重复渲染）。
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.ai_runtime import AIOrchestrator
from core.character_session import CharacterSessionRegistry
from core.event_mailbox import InMemoryEventMailbox
from core.model_manager import ModelManager
from core.scope_actor_dispatcher import ScopeActorDispatcher
from core.task_ingress_router import TaskIngressRouter


def _runtime():
    return object.__new__(AIOrchestrator)


def _task(task_id: str, **overrides):
    base = {
        'id': task_id,
        'schedule': '0 7 * * *',
        'instruction': '测试任务',
        'enabled': True,
        'created_at': 0.0,
        'last_run': None,
        'next_run': 9999999999.0,
        'creator_scope': 'private:9',
    }
    base.update(overrides)
    return base


class RoundRobinDisplayNoAdvanceTests(unittest.TestCase):
    def _manager(self):
        config = {
            'upstreams': [
                {'name': 'up', 'base_url': 'https://a', 'api_key': 'k', 'messages_path': '/v1/messages'},
            ],
            'channels': [
                {'name': 'ch', 'strategy': 'roundrobin', 'models': [
                    {'upstream': 'up', 'model_id': 'm1'},
                    {'upstream': 'up', 'model_id': 'm2'},
                    {'upstream': 'up', 'model_id': 'm3'},
                ]},
            ],
            'roles': {'main': 'ch'},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'models_config.json'
            path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            return ModelManager(str(path))

    def test_readonly_display_does_not_advance_roundrobin(self):
        mm = self._manager()
        # 只读展示不推进轮询
        for _ in range(10):
            self.assertEqual(mm.get_model_for_role('main', advance=False)['model_name'], 'm1')
        # 真实请求仍从索引 0 开始（展示未污染轮询状态）
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'm1')

    def test_real_request_advances_roundrobin(self):
        mm = self._manager()
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'm1')
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'm2')
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'm3')
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'm1')


class SyncChannelIndexTests(unittest.TestCase):
    def _manager(self, strategy='fallback'):
        config = {
            'upstreams': [
                {'name': 'up-a', 'base_url': 'https://a', 'api_key': 'k', 'messages_path': '/v1/messages'},
                {'name': 'up-b', 'base_url': 'https://b', 'api_key': 'k', 'messages_path': '/v1/messages'},
            ],
            'channels': [
                {'name': 'ch', 'strategy': strategy, 'models': [
                    {'upstream': 'up-a', 'model_id': 'm1'},
                    {'upstream': 'up-b', 'model_id': 'm2'},
                    {'upstream': 'up-a', 'model_id': 'm3'},
                ]},
            ],
            'roles': {'main': 'ch'},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'models_config.json'
            path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            return ModelManager(str(path))

    def test_sync_channel_index_moves_fallback_to_model(self):
        mm = self._manager()
        mm.notify_failure('main')  # fallback → m2 (idx 1)
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'm2')
        mm.sync_channel_index('ch', 'm3')
        # 手动切到 m3 后，索引同步到 m3，下次读取不再显示旧模型
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'm3')

    def test_sync_channel_index_unknown_model_resets_to_zero(self):
        mm = self._manager()
        mm.notify_failure('main')  # idx 1
        mm.sync_channel_index('ch', 'no-such-model')
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'm1')

    def test_sync_channel_index_resets_fallback_reset_and_roundrobin(self):
        mm = self._manager(strategy='fallback_reset')
        mm.sync_channel_index('ch', 'm2')
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'm2')
        self.assertEqual(mm._request_fb_indexes['ch'], 1)

    def test_switch_channel_model_syncs_index(self):
        runtime = _runtime()
        mm = Mock()
        mm.get_role_channel_name = Mock(return_value='ch')
        mm.resolve_exact_model = Mock(return_value={
            'base_url': 'https://a', 'api_key': 'k', 'model_name': 'm3',
            'messages_path': '/v1/messages', 'display_name': 'up-a/m3',
        })
        mm.sync_channel_index = Mock()
        runtime.model_manager = mm
        runtime._update_model_from_config = Mock()
        ok, _msg = runtime.switch_channel_model('ch', 'up-a', 'm3')
        self.assertTrue(ok)
        mm.sync_channel_index.assert_called_once_with('ch', 'm3')


class RecurringSchedulerDebounceTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, tasks):
        runtime = _runtime()
        runtime._recurring_tasks = dict(tasks)
        runtime._save_recurring_tasks = Mock()
        runtime._submit_message = Mock()
        factory = Mock()
        factory.build_recurring_message.return_value = object()  # 非 None，表示可投递
        runtime._get_timed_event_messages = Mock(return_value=factory)
        return runtime

    async def _run_once(self, runtime):
        """让调度循环只迭代一次：第一次 sleep 返回，第二次取消退出循环。"""
        sleeps = iter([None])

        async def fake_sleep(_s):
            try:
                next(sleeps)
            except StopIteration:
                raise asyncio.CancelledError

        with patch.object(asyncio, 'sleep', side_effect=fake_sleep):
            await runtime._recurring_scheduler_loop()

    async def test_invalid_cron_disables_task(self):
        tid = 'abcdef1234567890abcdef1234567890'
        runtime = self._runtime({tid: _task(tid, schedule='garbage cron', next_run=0)})
        await self._run_once(runtime)
        task = runtime._recurring_tasks[tid]
        self.assertFalse(task['enabled'])
        self.assertIn('invalid cron', task['schedule_error'])
        runtime._save_recurring_tasks.assert_called_once()

    async def test_invalid_target_scope_disables_task(self):
        tid = 'abcdef1234567890abcdef1234567890'
        runtime = self._runtime({tid: _task(tid, target_scope='no-colon', next_run=0)})
        runtime._get_timed_event_messages.return_value.build_recurring_message.return_value = None
        await self._run_once(runtime)
        task = runtime._recurring_tasks[tid]
        self.assertFalse(task['enabled'])
        self.assertEqual(task['schedule_error'], 'invalid target_scope')

    async def test_valid_cron_advances_next_run_and_keeps_enabled(self):
        tid = 'abcdef1234567890abcdef1234567890'
        runtime = self._runtime({tid: _task(tid, next_run=0)})
        await self._run_once(runtime)
        task = runtime._recurring_tasks[tid]
        self.assertTrue(task['enabled'])
        self.assertGreater(task['next_run'], 0)
        self.assertIsNotNone(task['last_run'])
        runtime._submit_message.assert_called_once()


class RouterFailureStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mailbox = InMemoryEventMailbox()
        self.sessions = CharacterSessionRegistry(mailbox=self.mailbox)
        self.consumed = []
        self.done = asyncio.Event()

        async def consume(scope_key, item):
            self.consumed.append((scope_key, dict(item)))
            self.done.set()

        self.dispatcher = ScopeActorDispatcher(
            mailbox=self.mailbox,
            sessions=self.sessions,
            consume=consume,
        )

    async def asyncTearDown(self):
        await self.dispatcher.close()

    async def test_failed_item_gets_status_and_error(self):
        queue = asyncio.Queue()
        errors = []

        async def load(task_id):
            if task_id == 'bad':
                raise RuntimeError('load failed')
            return {}

        router = TaskIngressRouter(
            queue=queue,
            dispatcher=self.dispatcher,
            load_task=load,
            resolve_scope=lambda _task: None,
            on_error=errors.append,
        )
        runner = asyncio.create_task(router.run())
        item = {'kind': 'task', 'task_id': 'bad'}
        await queue.put(item)
        await queue.put({'kind': 'task', 'task_id': 'good'})

        await asyncio.wait_for(queue.join(), 1)
        await asyncio.wait_for(self.done.wait(), 1)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

        self.assertEqual([str(exc) for exc in errors], ['load failed'])
        # 失败任务原地落失败状态，不再静默丢失
        self.assertEqual(item['status'], 'failed')
        self.assertEqual(item['error'], 'load failed')
        # 后续任务不受影响
        self.assertEqual(self.consumed[0][0], 'task:good')


class DiarySliceTests(unittest.TestCase):
    def test_strips_trigger_entries_by_message_id(self):
        history = [
            {'message_id': 'm1', 'text': 'a'},
            {'message_id': 'm2', 'text': 'b'},
            {'message_id': 'm3', 'text': 'c'},
        ]
        triggers = [{'message_id': 'm3', 'text': 'c'}, {'message_id': 'm2', 'text': 'b'}]
        out = AIOrchestrator._strip_trigger_entries_from_history(history, triggers)
        self.assertEqual([e['message_id'] for e in out], ['m1'])

    def test_keeps_history_when_trigger_ids_not_in_history(self):
        # diary 快照不含触发消息时不再按条数切尾（旧逻辑会误删用户消息）
        history = [{'message_id': 'm1'}, {'message_id': 'm2'}]
        triggers = [{'message_id': 'zz-not-in-history', 'text': 'x'}]
        out = AIOrchestrator._strip_trigger_entries_from_history(history, triggers)
        self.assertEqual([e['message_id'] for e in out], ['m1', 'm2'])

    def test_falls_back_to_tail_slice_when_no_message_ids(self):
        # 系统事件无 message_id（闹钟/循环任务），保留旧语义按条数切尾
        history = [{'message_id': 'm1'}, {'message_id': None}]
        triggers = [{'message_id': None, 'text': 'sys'}]
        out = AIOrchestrator._strip_trigger_entries_from_history(history, triggers)
        self.assertEqual(len(out), 1)


if __name__ == '__main__':
    unittest.main()
