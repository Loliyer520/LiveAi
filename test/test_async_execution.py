import asyncio
import threading
import unittest

from core.async_execution import AsyncExecutionPool


class AsyncExecutionPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_pools_do_not_block_each_other(self):
        chat = AsyncExecutionPool('test-chat', 1)
        background = AsyncExecutionPool('test-background', 1)
        blocked = threading.Event()
        release = threading.Event()

        def blocking_background():
            blocked.set()
            release.wait(timeout=2)
            return 'background'

        background_future = asyncio.create_task(background.run(blocking_background))
        await asyncio.to_thread(blocked.wait, 1)
        chat_result = await asyncio.wait_for(chat.run(lambda: 'chat'), timeout=0.5)

        self.assertEqual(chat_result, 'chat')
        release.set()
        self.assertEqual(await background_future, 'background')
        chat.close()
        background.close()

    async def test_pool_enforces_its_own_worker_limit(self):
        pool = AsyncExecutionPool('test-limit', 1)
        release = threading.Event()
        started = []

        def job(name):
            started.append(name)
            if name == 'first':
                release.wait(timeout=2)
            return name

        first = asyncio.create_task(pool.run(job, 'first'))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(pool.run(job, 'second'))
        await asyncio.sleep(0.05)
        self.assertEqual(started, ['first'])
        release.set()
        self.assertEqual(await first, 'first')
        self.assertEqual(await second, 'second')
        pool.close()


if __name__ == '__main__':
    unittest.main()
