import asyncio
import unittest

from core.ai_runtime import AIOrchestrator
from core.character_session import CharacterSessionRegistry
from core.event_adapters import envelope_from_scope_turn_item
from core.event_batch_coordinator import AtomicTurnBatchCoordinator
from core.event_mailbox import InMemoryEventMailbox
from core.events import ChatMessage


def pending(text, message_id, *, stale=False):
    return {
        'kind': 'message',
        'message': ChatMessage(
            chat_type='group', chat_id=7, user_id=message_id, text=text,
            raw_message=text, sender={'nickname': text}, message_id=message_id,
            timestamp=1.0 if stale else 9999999999.0,
        ),
        'cleaned': text,
        'agent_id': 'agent-1',
        'scope_key': 'group:7',
        'deferred_count': 1,
        'trigger_messages': [{'text': text}],
    }


def append(runtime, item):
    runtime._event_mailbox.append(envelope_from_scope_turn_item(item), transient=item)


class BatchHandoffRuntimeTests(unittest.TestCase):
    def runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._event_mailbox = InMemoryEventMailbox()
        runtime._turn_batch_coordinator = AtomicTurnBatchCoordinator(runtime._event_mailbox)
        runtime._character_sessions = CharacterSessionRegistry(mailbox=runtime._event_mailbox)
        runtime._character_sessions.activate('group:7')
        runtime._message_epoch = 1
        runtime.queue = asyncio.Queue()
        runtime._stale_message_max_age = 300
        return runtime

    def completed_item(self):
        return {
            'scope_key': 'group:7',
            'message_epoch': 1,
            'followup_history_seed': [{'role': 'assistant', 'content': 'committed'}],
            'turn_commit_evidence': {
                'outbound_history_committed': True,
                'turn_log_committed': True,
                'turn_metadata_committed': True,
            },
            'completed_turn_metadata': {'agent_id': 'agent-1'},
        }

    def test_live_batch_is_atomic_and_later_arrival_waits(self):
        runtime = self.runtime()
        first, second, later = pending('one', 1), pending('two', 2), pending('later', 3)
        append(runtime, first)
        append(runtime, second)
        original_stale = runtime._is_message_stale
        seen = {'count': 0}

        def stale(message):
            seen['count'] += 1
            if seen['count'] == 1:
                append(runtime, later)
            return original_stale(message)

        runtime._is_message_stale = stale
        followup = runtime._handoff_completed_scope_turn(self.completed_item())
        self.assertEqual([entry['text'] for entry in followup['trigger_messages']], ['one', 'two'])
        self.assertEqual(runtime._event_mailbox.pending_count('group:7'), 1)
        self.assertIs(runtime._pop_pending_scope_turn('group:7'), later)

    def test_all_stale_promotes_task_before_release(self):
        runtime = self.runtime()
        append(runtime, pending('stale', 1, stale=True))
        runtime._character_sessions.append_pending_task(
            'group:7',
            {'kind': 'task', 'task_id': 'task-1', 'scope_key': 'group:7'},
        )
        result = runtime._handoff_completed_scope_turn(self.completed_item())
        self.assertIsNone(result)
        promoted = runtime.queue.get_nowait()
        self.assertEqual(promoted['task_id'], 'task-1')
        self.assertTrue(runtime._character_sessions.is_active('group:7'))

    def test_empty_batch_releases_scope(self):
        runtime = self.runtime()
        result = runtime._handoff_completed_scope_turn(self.completed_item())
        self.assertIsNone(result)
        self.assertFalse(runtime._character_sessions.is_active('group:7'))

    def test_tool_raw_pop_stays_single_and_identity_preserving(self):
        runtime = self.runtime()
        first, second = pending('one', 1), pending('two', 2)
        append(runtime, first)
        append(runtime, second)
        self.assertIs(runtime._pop_pending_scope_turn('group:7'), first)
        self.assertEqual(runtime._event_mailbox.pending_count('group:7'), 1)
        self.assertIs(runtime._pop_pending_scope_turn('group:7'), second)


if __name__ == '__main__':
    unittest.main()
