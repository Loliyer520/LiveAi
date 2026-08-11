import asyncio
import unittest

from core.ai_runtime import AIOrchestrator
from core.character_session import CharacterSessionRegistry
from core.event_adapters import envelope_from_scope_turn_item
from core.event_batch_coordinator import AtomicTurnBatchCoordinator
from core.event_mailbox import InMemoryEventMailbox
from core.events import ChatMessage


def message_item(text='one', message_id=1):
    return {
        'kind': 'message',
        'message': ChatMessage(
            chat_type='group', chat_id=7, user_id=message_id, text=text,
            raw_message=text, sender={'nickname': text}, message_id=message_id,
            timestamp=9999999999.0,
        ),
        'cleaned': text,
        'agent_id': 'agent-1',
        'scope_key': 'group:7',
        'message_epoch': 1,
        'trigger_messages': [{'text': text}],
    }


class ActiveOwnerRuntimeHarnessTests(unittest.TestCase):
    def runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._event_mailbox = InMemoryEventMailbox()
        runtime._character_sessions = CharacterSessionRegistry(mailbox=runtime._event_mailbox)
        runtime._turn_batch_coordinator = AtomicTurnBatchCoordinator(runtime._event_mailbox)
        runtime._pending_self_interrupts = {}
        runtime._group_reply_windows = {}
        runtime._message_epoch = 1
        runtime._stale_message_max_age = 300
        runtime.queue = asyncio.Queue()
        return runtime

    def test_message_turn_entry_normal_release_exception_and_epoch_paths(self):
        runtime = self.runtime()
        first = message_item('one', 1)
        second = message_item('two', 2)
        self.assertTrue(runtime._reserve_scope_turn(first))
        self.assertFalse(runtime._reserve_scope_turn(second))
        followup = runtime._release_scope_turn(first)
        self.assertIsNotNone(followup)
        self.assertTrue(runtime._scope_turn_is_active('group:7'))
        runtime._message_epoch = 2
        followup['message_epoch'] = 1
        self.assertIsNone(runtime._release_scope_turn(followup))
        self.assertFalse(runtime._scope_turn_is_active('group:7'))

    def test_batch_live_retains_active_and_empty_promotes_task_then_releases(self):
        runtime = self.runtime()
        active = message_item('active', 1)
        pending = message_item('pending', 2)
        runtime._activate_scope_turn('group:7')
        runtime._append_pending_scope_turn('group:7', pending)
        active['followup_history_seed'] = [{'role': 'assistant'}]
        active['completed_turn_metadata'] = {'agent_id': 'agent-1'}
        followup = runtime._handoff_completed_scope_turn(active)
        self.assertIsNotNone(followup)
        self.assertTrue(runtime._scope_turn_is_active('group:7'))
        runtime._character_sessions.append_pending_task(
            'group:7',
            {'kind': 'task', 'task_id': 'task-1', 'message_epoch': runtime._message_epoch},
        )
        self.assertIsNone(runtime._handoff_completed_scope_turn(followup))
        self.assertTrue(runtime._scope_turn_is_active('group:7'))
        self.assertEqual(runtime.queue.get_nowait()['task_id'], 'task-1')
        self.assertIsNone(runtime._handoff_completed_scope_turn(followup))
        self.assertFalse(runtime._scope_turn_is_active('group:7'))

    def test_task_and_message_reservations_contend_for_same_active_owner(self):
        runtime = self.runtime()
        runtime._activate_scope_turn('group:7')
        task = {'task_id': 'task-1', 'message_epoch': 1}
        self.assertFalse(runtime._reserve_task_scope('group:7', task))
        runtime._deactivate_scope_turn('group:7')
        self.assertTrue(runtime._reserve_task_scope('group:7', task))
        self.assertFalse(runtime._reserve_scope_turn(message_item('message', 1)))

    def test_agent_report_active_only_and_debounce_busy_contract(self):
        runtime = self.runtime()
        pending = message_item('pending', 1)
        runtime._append_pending_scope_turn('group:7', pending)
        self.assertFalse(runtime._scope_turn_is_active('group:7'))
        self.assertTrue(runtime._scope_turn_is_busy('group:7'))
        runtime._activate_scope_turn('group:7')
        self.assertTrue(runtime._scope_turn_is_active('group:7'))

    def test_cancel_clear_and_cross_scope_reactivation(self):
        runtime = self.runtime()
        runtime._activate_scope_turn('group:7')
        runtime._activate_scope_turn('private:9')
        runtime._cancel_active_requests()
        self.assertFalse(runtime._scope_turn_is_active('group:7'))
        self.assertFalse(runtime._scope_turn_is_active('private:9'))
        self.assertTrue(runtime._reserve_scope_turn(message_item('again', 3)))

    def test_runtime_activate_wrapper_is_idempotent_like_set_add(self):
        runtime = self.runtime()
        runtime._activate_scope_turn('group:7')
        runtime._activate_scope_turn('group:7')
        self.assertTrue(runtime._scope_turn_is_active('group:7'))
        runtime._deactivate_scope_turn('group:7')
        self.assertFalse(runtime._scope_turn_is_active('group:7'))


if __name__ == '__main__':
    unittest.main()
