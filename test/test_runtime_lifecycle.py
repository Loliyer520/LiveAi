import asyncio
import threading
import unittest
from unittest.mock import Mock

from core.ai_runtime import AIOrchestrator
from pack.napcat import NapcatBot


class RuntimeLifecycleTests(unittest.TestCase):
    def test_stop_closes_loop_state_and_execution_pools(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.loop = asyncio.new_event_loop()
        runtime.queue = object()
        runtime.ready = threading.Event()
        runtime.ready.set()
        loop_started = threading.Event()
        runtime.loop.call_soon(loop_started.set)
        runtime.thread = threading.Thread(target=runtime.loop.run_forever, daemon=True)
        runtime.thread.start()
        self.assertTrue(loop_started.wait(timeout=1))

        actor_closed = threading.Event()

        async def close_dispatcher():
            actor_closed.set()

        runtime._scope_dispatcher = Mock()
        runtime._scope_dispatcher.close = close_dispatcher
        runtime.agent_manager = Mock()
        runtime._chat_model_pool = Mock()
        runtime._runtime_io_pool = Mock()
        runtime._background_pool = Mock()

        runtime.stop()

        self.assertTrue(actor_closed.is_set())
        self.assertIsNone(runtime.thread)
        self.assertIsNone(runtime.loop)
        self.assertIsNone(runtime.queue)
        self.assertFalse(runtime.ready.is_set())
        runtime.agent_manager.set_loop.assert_called_once_with(None)
        runtime.agent_manager.set_blocking_runner.assert_called_once_with(None)
        runtime._chat_model_pool.close.assert_called_once_with()
        runtime._runtime_io_pool.close.assert_called_once_with()
        runtime._background_pool.close.assert_called_once_with()

    def test_napcat_runs_shutdown_callbacks_in_reverse_registration_order(self):
        bot = NapcatBot('ws://example', 'http://example', 1)
        calls = []
        bot.on_shutdown(lambda: calls.append('runtime'))
        bot.on_shutdown(lambda: calls.append('webui'))

        class FakeWebSocketApp:
            def run_forever(self):
                calls.append('websocket')

        bot.ws = None
        with unittest.mock.patch('pack.napcat.websocket.WebSocketApp', return_value=FakeWebSocketApp()):
            bot.start()

        self.assertEqual(calls, ['websocket', 'webui', 'runtime'])


if __name__ == '__main__':
    unittest.main()
