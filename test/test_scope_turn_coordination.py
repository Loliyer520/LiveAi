import asyncio
import time
import unittest
from types import SimpleNamespace

from core.ai_runtime import AIOrchestrator
from core.event_batch_coordinator import AtomicTurnBatchCoordinator
from core.event_mailbox import InMemoryEventMailbox
from core.character_session import CharacterSessionRegistry
from core.events import ChatMessage


def message(text, message_id, timestamp=None):
    return ChatMessage(
        chat_type='group',
        chat_id=7,
        user_id=9,
        text=text,
        raw_message=text,
        sender={'nickname': 'user'},
        message_id=message_id,
        timestamp=time.time() if timestamp is None else timestamp,
    )


def item(text, message_id, timestamp=None):
    return {
        'kind': 'message',
        'message': message(text, message_id, timestamp),
        'cleaned': text,
        'agent_id': 'agent-1',
        'message_epoch': 4,
        'trigger_messages': [{'text': text}],
    }


class ScopeTurnCoordinationCharacterizationTests(unittest.TestCase):
    def runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._event_mailbox = InMemoryEventMailbox()
        runtime._turn_batch_coordinator = AtomicTurnBatchCoordinator(runtime._event_mailbox)
        runtime._character_sessions = CharacterSessionRegistry(mailbox=runtime._event_mailbox)
        runtime._group_reply_windows = {}
        runtime._message_epoch = 4
        runtime._stale_message_max_age = 300
        runtime.queue = asyncio.Queue()
        return runtime

    def test_pending_primitives_preserve_fifo_count_and_busy_state(self):
        runtime = self.runtime()
        scope = 'group:7'
        first, second = item('one', 1), item('two', 2)
        runtime._append_pending_scope_turn(scope, first)
        runtime._append_pending_scope_turn(scope, second)
        self.assertTrue(runtime._scope_turn_has_pending(scope))
        self.assertTrue(runtime._scope_turn_is_busy(scope))
        self.assertEqual(runtime._pending_scope_turn_count(scope), 2)
        self.assertIs(runtime._pop_pending_scope_turn(scope), first)
        self.assertIs(runtime._pop_pending_scope_turn(scope), second)
        self.assertFalse(runtime._scope_turn_has_pending(scope))

    def test_reserve_take_release_preserve_single_owner_fifo_and_history_seed(self):
        runtime = self.runtime()
        scope = 'group:7'
        first, second, third = item('one', 1), item('two', 2), item('three', 3)
        self.assertTrue(runtime._reserve_scope_turn(first))
        self.assertFalse(runtime._reserve_scope_turn(second))
        self.assertFalse(runtime._reserve_scope_turn(third))
        self.assertTrue(runtime._scope_turn_is_active(scope))
        first['followup_history_seed'] = [{'role': 'assistant', 'content': 'saved'}]
        followup = runtime._take_pending_scope_turn(first)
        self.assertEqual(followup['cleaned'], 'two')
        followup['followup_history_seed'] = first['followup_history_seed']
        released = runtime._release_scope_turn(followup)
        self.assertEqual(released['cleaned'], 'three')
        self.assertEqual(released['history_seed'], first['followup_history_seed'])
        self.assertIsNone(runtime._release_scope_turn(released))
        self.assertFalse(runtime._scope_turn_is_active(scope))

    def test_live_pop_skips_stale_but_raw_pop_preserves_it(self):
        runtime = self.runtime()
        scope = 'group:7'
        stale = item('stale', 1, timestamp=1.0)
        live = item('live', 2)
        runtime._append_pending_scope_turn(scope, stale)
        runtime._append_pending_scope_turn(scope, live)
        self.assertEqual(runtime._pop_next_live_pending_scope_turn(scope)['cleaned'], 'live')
        runtime._append_pending_scope_turn(scope, stale)
        self.assertEqual(runtime._pop_pending_scope_turn(scope)['cleaned'], 'stale')

    def test_task_release_promotes_message_before_task(self):
        runtime = self.runtime()
        scope = 'group:7'
        runtime._activate_scope_turn(scope)
        pending_message = item('message-first', 1)
        runtime._append_pending_scope_turn(scope, pending_message)
        runtime._character_sessions.append_pending_task(
            scope,
            {'kind': 'task', 'task_id': 'task-1', 'message_epoch': 4},
        )
        runtime._release_task_scope(scope)
        self.assertIs(runtime.queue.get_nowait(), pending_message)
        self.assertEqual(runtime._character_sessions.pending_task_count(scope), 1)
        self.assertTrue(runtime._scope_turn_is_active(scope))

    def test_clear_removes_active_pending_tasks_and_interrupts(self):
        runtime = self.runtime()
        scope = 'group:7'
        runtime._activate_scope_turn(scope)
        runtime._append_pending_scope_turn(scope, item('pending', 1))
        runtime._character_sessions.append_pending_task(scope, {'kind': 'task'})
        runtime._cancel_active_requests()
        self.assertEqual(runtime._message_epoch, 5)
        self.assertFalse(runtime._scope_turn_is_busy(scope))
        self.assertEqual(runtime._character_sessions.pending_task_count(scope), 0)

    def test_debounce_busy_contract_includes_active_or_pending(self):
        runtime = self.runtime()
        scope = 'group:7'
        self.assertFalse(runtime._scope_turn_is_busy(scope))
        runtime._activate_scope_turn(scope)
        self.assertTrue(runtime._scope_turn_is_busy(scope))
        runtime._deactivate_scope_turn(scope)
        runtime._append_pending_scope_turn(scope, item('pending', 1))
        self.assertTrue(runtime._scope_turn_is_busy(scope))

    def test_agent_report_idle_contract_checks_active_only(self):
        runtime = self.runtime()
        scope = 'group:7'
        runtime._append_pending_scope_turn(scope, item('pending', 1))
        self.assertFalse(runtime._scope_turn_is_active(scope))
        self.assertTrue(runtime._scope_turn_has_pending(scope))
        runtime._activate_scope_turn(scope)
        self.assertTrue(runtime._scope_turn_is_active(scope))

    def test_tool_loop_raw_pending_read_consumes_one_without_stale_filter(self):
        runtime = self.runtime()
        scope = 'group:7'
        stale = item('stale-tool-reminder', 1, timestamp=1.0)
        second = item('second', 2)
        runtime._append_pending_scope_turn(scope, stale)
        runtime._append_pending_scope_turn(scope, second)
        folded = runtime._pop_pending_scope_turn(scope)
        self.assertIs(folded, stale)
        self.assertEqual(runtime._pending_scope_turn_count(scope), 1)

    def test_fold_silent_head_merges_later_visible_mailbox_batch(self):
        runtime = self.runtime()
        scope = 'group:7'
        silent = item('self-sent', 1)
        silent['silent_event'] = True
        runtime._append_pending_scope_turn(scope, item('visible', 2))

        folded = runtime._fold_silent_followup_head(scope, silent)

        self.assertEqual([entry['text'] for entry in folded['trigger_messages']], ['self-sent', 'visible'])
        self.assertEqual(
            [bool(entry.get('silent_event')) for entry in folded['batch_items']],
            [True, False],
        )

    def test_fold_silent_head_drops_silent_only_mailbox_batch(self):
        runtime = self.runtime()
        silent = item('self-sent', 1)
        silent['silent_event'] = True

        self.assertIsNone(runtime._fold_silent_followup_head('group:7', silent))


if __name__ == '__main__':
    unittest.main()
