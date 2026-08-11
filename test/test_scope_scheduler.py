import unittest

from core.event_adapters import envelope_from_scope_turn_item
from core.event_batch_coordinator import CompletedTurn
from core.events import ChatMessage
from core.scope_scheduler import ScopeScheduler


def item(text, message_id, *, stale=False):
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


def envelope(value):
    return envelope_from_scope_turn_item(value)


class ScopeSchedulerTests(unittest.TestCase):
    def test_reserve_single_consumer_then_fifo_append(self):
        scheduler = ScopeScheduler()
        first, second = item('one', 1), item('two', 2)
        self.assertTrue(scheduler.reserve_or_append(envelope(first), transient=first))
        self.assertFalse(scheduler.reserve_or_append(envelope(second), transient=second))
        self.assertTrue(scheduler.session('group', '7').is_active())
        self.assertIs(scheduler.pop_tool_raw('group', '7'), second)

    def test_completed_batch_precedes_task(self):
        scheduler = ScopeScheduler()
        first, second = item('one', 1), item('two', 2)
        scheduler.session('group', '7').activate()
        scheduler.append_while_active(envelope(first), transient=first)
        scheduler.append_while_active(envelope(second), transient=second)
        scheduler.append_task('group', '7', {'task_id': 'task-1'})
        result = scheduler.handoff_completed_turn(
            CompletedTurn('group:7', history_seed=({'role': 'assistant'},)),
            is_stale=lambda _value: False,
        )
        self.assertIsNotNone(result.followup)
        self.assertIsNone(result.promoted_task)
        self.assertFalse(result.released)
        self.assertEqual([entry['text'] for entry in result.followup['trigger_messages']], ['one', 'two'])
        self.assertEqual(scheduler.session('group', '7').pending_task_count(), 1)

    def test_all_stale_promotes_task_and_empty_releases(self):
        scheduler = ScopeScheduler()
        session = scheduler.session('group', '7')
        session.activate()
        stale = item('stale', 1, stale=True)
        scheduler.append_while_active(envelope(stale), transient=stale)
        scheduler.append_task('group', '7', {'task_id': 'task-1'})
        promoted = scheduler.handoff_completed_turn(
            CompletedTurn('group:7'), is_stale=lambda _value: True,
        )
        self.assertEqual(promoted.promoted_task['task_id'], 'task-1')
        self.assertFalse(promoted.released)
        released = scheduler.handoff_completed_turn(
            CompletedTurn('group:7'), is_stale=lambda _value: False,
        )
        self.assertTrue(released.released)
        self.assertFalse(session.is_active())

    def test_tool_raw_pop_keeps_identity_and_no_stale_filter(self):
        scheduler = ScopeScheduler()
        session = scheduler.session('group', '7')
        session.activate()
        stale, live = item('stale', 1, stale=True), item('live', 2)
        scheduler.append_while_active(envelope(stale), transient=stale)
        scheduler.append_while_active(envelope(live), transient=live)
        self.assertIs(scheduler.pop_tool_raw('group', '7'), stale)
        self.assertIs(scheduler.pop_tool_raw('group', '7'), live)

    def test_different_scopes_are_independent(self):
        scheduler = ScopeScheduler()
        group = item('group', 1)
        private = item('private', 2)
        private['message'].chat_type = 'private'
        private['message'].chat_id = 9
        private['scope_key'] = 'private:9'
        self.assertTrue(scheduler.reserve_or_append(envelope(group), transient=group))
        self.assertTrue(scheduler.reserve_or_append(envelope(private), transient=private))
        self.assertTrue(scheduler.session('group', '7').is_active())
        self.assertTrue(scheduler.session('private', '9').is_active())


if __name__ == '__main__':
    unittest.main()
