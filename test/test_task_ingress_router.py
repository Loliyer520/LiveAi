import asyncio
import unittest

from core.character_session import CharacterSessionRegistry
from core.event_mailbox import InMemoryEventMailbox
from core.scope_actor_dispatcher import ScopeActorDispatcher
from core.task_ingress_router import TaskIngressRouter


class TaskIngressRouterTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_targeted_task_routes_to_scope_actor(self):
        queue = asyncio.Queue()
        router = TaskIngressRouter(
            queue=queue,
            dispatcher=self.dispatcher,
            load_task=lambda _task_id: asyncio.sleep(0, result={'target': 'group:7'}),
            resolve_scope=lambda task: task.get('target'),
        )
        item = {'kind': 'task', 'task_id': 'abc', 'message_epoch': 1}

        scope_key = await router.route(item)
        await asyncio.wait_for(self.done.wait(), 1)

        self.assertEqual(scope_key, 'group:7')
        self.assertTrue(item['scope_prereserved'])
        self.assertEqual(item['scope_key'], 'group:7')
        self.assertEqual(self.consumed[0][0], 'group:7')

    async def test_background_task_gets_independent_actor_scope(self):
        queue = asyncio.Queue()
        router = TaskIngressRouter(
            queue=queue,
            dispatcher=self.dispatcher,
            load_task=lambda _task_id: asyncio.sleep(0, result={}),
            resolve_scope=lambda _task: None,
        )
        item = {'kind': 'task', 'task_id': 'abc'}

        scope_key = await router.route(item)
        await asyncio.wait_for(self.done.wait(), 1)

        self.assertEqual(scope_key, 'task:abc')
        self.assertNotIn('scope_prereserved', item)
        self.assertEqual(self.consumed[0][0], 'task:abc')

    async def test_stale_and_non_task_items_are_ignored_without_loading(self):
        queue = asyncio.Queue()
        loads = []

        async def load(task_id):
            loads.append(task_id)
            return {}

        router = TaskIngressRouter(
            queue=queue,
            dispatcher=self.dispatcher,
            load_task=load,
            resolve_scope=lambda _task: None,
            is_stale=lambda item: item.get('message_epoch') == 0,
        )

        self.assertIsNone(await router.route({'kind': 'task', 'task_id': 'old', 'message_epoch': 0}))
        self.assertIsNone(await router.route({'kind': 'message', 'task_id': 'not-task'}))
        self.assertEqual(loads, [])
        self.assertEqual(self.consumed, [])

    async def test_run_reports_error_and_continues_to_next_item(self):
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
        await queue.put({'kind': 'task', 'task_id': 'bad'})
        await queue.put({'kind': 'task', 'task_id': 'good'})

        await asyncio.wait_for(queue.join(), 1)
        await asyncio.wait_for(self.done.wait(), 1)
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

        self.assertEqual([str(exc) for exc in errors], ['load failed'])
        self.assertEqual(self.consumed[0][0], 'task:good')


if __name__ == '__main__':
    unittest.main()
