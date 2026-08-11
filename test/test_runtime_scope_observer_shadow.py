import asyncio
import unittest

from core.ai_runtime import AIOrchestrator
from core.character_session import CharacterSessionRegistry
from core.event_adapters import envelope_from_scope_turn_item
from core.event_mailbox import InMemoryEventMailbox
from core.events import ChatMessage
from core.runtime_scope_observer import RuntimeScopeObserver


def item():
    return {
        'kind': 'message',
        'message': ChatMessage(
            chat_type='group', chat_id=7, user_id=1, text='hello',
            raw_message='hello', sender={'nickname': 'user'}, message_id=1,
            timestamp=1.0,
        ),
        'cleaned': 'hello',
        'agent_id': 'agent-1',
        'scope_key': 'group:7',
        'trigger_messages': [{'text': 'hello'}],
    }


class RuntimeScopeObserverShadowIntegrationTests(unittest.TestCase):
    def runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._event_mailbox = InMemoryEventMailbox()
        runtime._character_sessions = CharacterSessionRegistry(mailbox=runtime._event_mailbox)
        runtime.queue = asyncio.Queue()
        runtime._runtime_scope_observer = RuntimeScopeObserver(
            is_active=runtime._character_sessions.is_active,
            mailbox=runtime._event_mailbox,
            pending_task_count=runtime._character_sessions.pending_task_count,
            queue_size=runtime.queue.qsize,
        )
        return runtime

    def test_observation_matches_current_owner_without_mutation(self):
        runtime = self.runtime()
        value = item()
        runtime._character_sessions.activate('group:7')
        runtime._event_mailbox.append(
            envelope_from_scope_turn_item(value), transient=value,
        )
        runtime._character_sessions.append_pending_task(
            'group:7', {'task_id': 'task-1'},
        )
        runtime.queue.put_nowait({'kind': 'message'})
        before = (
            tuple(runtime._character_sessions.list_scope_keys()),
            runtime._event_mailbox.pending_count('group:7'),
            runtime._character_sessions.pending_task_count('group:7'),
            runtime.queue.qsize(),
        )
        observation = runtime.observe_runtime_scope_by_key('group:7')
        self.assertEqual(observation['scope_key'], 'group:7')
        self.assertTrue(observation['active'])
        self.assertEqual(observation['pending_event_count'], 1)
        self.assertEqual(observation['pending_task_count'], 1)
        self.assertEqual(observation['runtime_queue_size'], 1)
        after = (
            tuple(runtime._character_sessions.list_scope_keys()),
            runtime._event_mailbox.pending_count('group:7'),
            runtime._character_sessions.pending_task_count('group:7'),
            runtime.queue.qsize(),
        )
        self.assertEqual(after, before)
        runtime._character_sessions.clear_pending_tasks(scope_key='group:7')
        self.assertEqual(runtime._character_sessions.pending_task_count('group:7'), 0)

    def test_observation_does_not_change_runtime_status_contract(self):
        runtime = self.runtime()
        runtime.observe_runtime_scope('group', '7')
        source = __import__('inspect').getsource(AIOrchestrator.get_runtime_status)
        self.assertNotIn('observe_runtime_scope', source)
        self.assertNotIn('runtime_scope', source)


if __name__ == '__main__':
    unittest.main()
