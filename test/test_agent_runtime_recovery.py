import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.ai_runtime import AIOrchestrator


class _FakeTask:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


class _FakeLoop:
    def __init__(self):
        self.created = []

    def create_task(self, coro):
        self.created.append(coro)
        coro.close()
        return _FakeTask(done=False)


class AgentRuntimeRecoveryTests(unittest.TestCase):
    def test_ensure_agent_loop_running_restarts_missing_task(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.loop = _FakeLoop()
        runtime.config = SimpleNamespace(agent_prompt_path='data/prompt/dev_agent.txt')
        runtime.agent_manager = MagicMock()
        runtime.agent_manager.get_agent_task.return_value = None
        runtime.agent_manager.get_agent_client.return_value = None
        runtime.agent_manager.on_agent_message = MagicMock()

        async def _fake_run_agent_loop(*_args, **_kwargs):
            return None

        runtime.agent_manager.run_agent_loop = MagicMock(side_effect=_fake_run_agent_loop)
        runtime._build_restored_agent_client = MagicMock(return_value='rebuilt-client')
        runtime._get_github_api_token = MagicMock(return_value='gh-token')
        runtime._get_ssh_profiles_map = MagicMock(return_value={'prod': 'cfg'})

        result = runtime._ensure_agent_loop_running(
            'agent-1',
            {'agent_id': 'agent-1', 'status': 'running'},
        )

        self.assertEqual(result, {'ok': True, 'started': True, 'error': None})
        runtime.agent_manager.register_agent_client.assert_called_once_with('agent-1', 'rebuilt-client')
        runtime.agent_manager.register_agent_task.assert_called_once()
        self.assertEqual(len(runtime.loop.created), 1)

    def test_ensure_agent_loop_running_keeps_live_task(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.loop = _FakeLoop()
        runtime.agent_manager = MagicMock()
        runtime.agent_manager.get_agent_task.return_value = _FakeTask(done=False)

        result = runtime._ensure_agent_loop_running('agent-1')

        self.assertEqual(result, {'ok': True, 'started': False, 'error': None})
        runtime.agent_manager.register_agent_client.assert_not_called()
        runtime.agent_manager.register_agent_task.assert_not_called()
        self.assertEqual(runtime.loop.created, [])


if __name__ == '__main__':
    unittest.main()
