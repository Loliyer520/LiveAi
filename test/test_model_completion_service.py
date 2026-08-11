import threading
import unittest

from core.async_execution import AsyncExecutionPool
from core.model_completion_service import ModelCompletionService
from pack.anthropic_chat_model import AnthropicChatModel, AnthropicReply


class ModelCompletionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.default_pool = AsyncExecutionPool('model-default-test', 1)
        self.other_pool = AsyncExecutionPool('model-other-test', 1)

    async def asyncTearDown(self):
        self.default_pool.close()
        self.other_pool.close()

    async def test_snapshot_keeps_original_client_after_provider_switch(self):
        first = AnthropicChatModel('https://first.example', model_name='first')
        second = AnthropicChatModel('https://second.example', model_name='second')
        current = {'client': first}
        calls = []

        def complete(*args, **kwargs):
            calls.append((threading.current_thread().name, args, kwargs))
            return AnthropicReply(text='ok')

        first.complete = complete
        service = ModelCompletionService(
            get_client=lambda: current['client'],
            default_pool=self.default_pool,
        )
        snapshot = service.snapshot()
        current['client'] = second

        reply = await service.complete(snapshot, [], [], None, 0.7, thinking='low')

        self.assertEqual(reply.text, 'ok')
        self.assertIs(snapshot.client, first)
        self.assertEqual(snapshot.model_name, 'first')
        self.assertEqual(snapshot.api_url, 'https://first.example/messages')
        self.assertTrue(calls[0][0].startswith('model-default-test'))
        self.assertEqual(calls[0][1][3], 'first')
        self.assertEqual(calls[0][2]['thinking'], 'low')

    async def test_explicit_pool_overrides_default_pool(self):
        client = AnthropicChatModel('https://example', model_name='model')
        thread_names = []

        def complete(*_args, **_kwargs):
            thread_names.append(threading.current_thread().name)
            return AnthropicReply(text='ok')

        client.complete = complete
        service = ModelCompletionService(
            get_client=lambda: client,
            default_pool=self.default_pool,
        )

        await service.complete(
            service.snapshot(),
            [],
            [],
            None,
            0.7,
            execution_pool=self.other_pool,
        )

        self.assertTrue(thread_names[0].startswith('model-other-test'))


if __name__ == '__main__':
    unittest.main()
