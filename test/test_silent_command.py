import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_runtime import AIOrchestrator


class SilentCommandTests(unittest.IsolatedAsyncioTestCase):
    def _build_runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(
            enabled=True,
            silent_mode=False,
            admin_qq='42',
            master_qq='99',
        )
        runtime.bot = SimpleNamespace(send_text=Mock())
        runtime._cancel_active_requests = Mock()
        runtime._is_admin_message = AIOrchestrator._is_admin_message.__get__(runtime, AIOrchestrator)
        runtime._is_master_private_message = AIOrchestrator._is_master_private_message.__get__(runtime, AIOrchestrator)
        runtime._is_message_allowed_by_power_mode = AIOrchestrator._is_message_allowed_by_power_mode.__get__(runtime, AIOrchestrator)
        runtime._handle_power_command = AIOrchestrator._handle_power_command.__get__(runtime, AIOrchestrator)
        return runtime

    async def test_silent_command_enables_master_private_only_mode(self):
        runtime = self._build_runtime()
        message = SimpleNamespace(user_id='42', chat_type='private', chat_id='42')

        await runtime._handle_power_command(message, '/silent')

        self.assertTrue(runtime.config.enabled)
        self.assertTrue(runtime.config.silent_mode)
        runtime._cancel_active_requests.assert_called_once()
        self.assertIn('静默模式', runtime.bot.send_text.call_args.args[2])

    async def test_on_command_exits_silent_mode(self):
        runtime = self._build_runtime()
        runtime.config.enabled = False
        runtime.config.silent_mode = True
        message = SimpleNamespace(user_id='42', chat_type='private', chat_id='42')

        await runtime._handle_power_command(message, '/on')

        self.assertTrue(runtime.config.enabled)
        self.assertFalse(runtime.config.silent_mode)

    def test_power_mode_allows_only_master_private_when_silent(self):
        runtime = self._build_runtime()
        runtime.config.silent_mode = True

        master_private = SimpleNamespace(chat_type='private', chat_id='99', user_id='99')
        other_private = SimpleNamespace(chat_type='private', chat_id='42', user_id='42')
        group_message = SimpleNamespace(chat_type='group', chat_id='777', user_id='99')

        self.assertTrue(runtime._is_message_allowed_by_power_mode(master_private))
        self.assertFalse(runtime._is_message_allowed_by_power_mode(other_private))
        self.assertFalse(runtime._is_message_allowed_by_power_mode(group_message))


if __name__ == '__main__':
    unittest.main()
