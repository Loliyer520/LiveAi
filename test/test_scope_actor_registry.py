import asyncio
import unittest

from core.scope_actor_registry import ScopeActorRegistry


class ScopeActorRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_has_one_consumer_and_different_keys_are_independent(self):
        started = []

        async def consumer(key, event):
            started.append(key)
            while True:
                await event.wait()
                event.clear()

        registry = ScopeActorRegistry(consumer)
        first = registry.ensure('group:1')
        second = registry.ensure('group:1')
        other = registry.ensure('private:2')
        await asyncio.sleep(0)

        self.assertIs(first, second)
        self.assertIsNot(first, other)
        self.assertEqual(started, ['group:1', 'private:2'])
        self.assertEqual(registry.active_count(), 2)

        await registry.close()
        self.assertEqual(registry.active_count(), 0)

    async def test_wake_delivers_signal_without_creating_duplicate_task(self):
        received = asyncio.Event()

        async def consumer(_key, event):
            while True:
                await event.wait()
                event.clear()
                received.set()

        registry = ScopeActorRegistry(consumer)
        task = registry.ensure('master:global')
        registry.wake('master:global')
        await asyncio.wait_for(received.wait(), timeout=1)

        self.assertIs(task, registry.ensure('master:global'))
        await registry.close()


if __name__ == '__main__':
    unittest.main()
