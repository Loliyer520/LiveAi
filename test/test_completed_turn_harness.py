import unittest

from core.event_adapters import envelope_from_scope_turn_item
from core.event_batch_coordinator import AtomicTurnBatchCoordinator, CompletedTurn
from core.event_mailbox import InMemoryEventMailbox
from core.events import ChatMessage
from support.completed_turn_harness import (
    CompletedTurnIntegrationHarness,
    assert_trigger_context_preserved,
    existing_turn_log_metadata,
)


def pending(text, message_id, *, image_ref=None, reply_to=None):
    trigger = {
        'text': text,
        'raw_message': text if image_ref is None else f'{text} [CQ:image,file={image_ref}]',
        'message_id': message_id,
        'source_label': 'QQ群消息',
        'source_kind': 'group',
        'raw_source': 'napcat',
        'reply_to': reply_to,
    }
    item = {
        'kind': 'message',
        'message': ChatMessage(
            chat_type='group', chat_id=7, user_id=message_id, text=text,
            raw_message=trigger['raw_message'], sender={'nickname': text},
            message_id=message_id, timestamp=float(message_id),
            raw_data={'source': 'napcat', 'reply_to': reply_to},
        ),
        'cleaned': text,
        'agent_id': 'agent-1',
        'scope_key': 'group:7',
        'deferred_count': 1,
        'trigger_messages': [trigger],
    }
    return item


def append(mailbox, item):
    mailbox.append(envelope_from_scope_turn_item(item), transient=item)


class CompletedTurnIntegrationHarnessTests(unittest.TestCase):
    def harness(self):
        mailbox = InMemoryEventMailbox()
        return mailbox, CompletedTurnIntegrationHarness(AtomicTurnBatchCoordinator(mailbox))

    def test_handoff_refuses_to_drain_before_all_commits(self):
        mailbox, harness = self.harness()
        append(mailbox, pending('queued', 1))
        harness.commit_current_turn(outbound=True, turn_log=True, metadata=False)
        with self.assertRaises(RuntimeError):
            harness.handoff(CompletedTurn('group:7'), is_stale=lambda _item: False)
        self.assertEqual(mailbox.pending_count('group:7'), 1)

    def test_completed_turn_drains_once_and_runs_followup(self):
        mailbox, harness = self.harness()
        first = pending('one', 1, image_ref='a.jpg')
        second = pending('two', 2, reply_to=1)
        append(mailbox, first)
        append(mailbox, second)
        metadata = existing_turn_log_metadata(
            agent_id='agent-1', temperature=0.85,
            turn_meta={'turn_kind': 'message'},
            tool_iterations=[{'assistant_text': 'thinking', 'tool_calls': []}],
            generation_ms=123,
        )
        harness.commit_current_turn(outbound=True, turn_log=True, metadata=True)
        result = harness.handoff(
            CompletedTurn(
                'group:7',
                history_seed=({'role': 'assistant', 'content': 'committed'},),
                metadata=metadata,
            ),
            is_stale=lambda _item: False,
        )
        self.assertIsNotNone(result.followup)
        self.assertIsNone(result.promoted_task)
        self.assertFalse(result.scope_released)
        self.assertEqual(mailbox.pending_count('group:7'), 0)
        self.assertEqual(
            [entry['text'] for entry in result.followup['trigger_messages']],
            ['one', 'two'],
        )
        assert_trigger_context_preserved(result.followup['trigger_messages'])
        self.assertIn('[CQ:image,file=a.jpg]', result.followup['trigger_messages'][0]['raw_message'])
        self.assertEqual(result.followup['trigger_messages'][1]['reply_to'], 1)
        self.assertEqual(result.followup['turn_metadata']['tool_iterations'][0]['assistant_text'], 'thinking')
        self.assertEqual(result.followup['history_seed'][0]['content'], 'committed')

    def test_empty_or_all_stale_batch_promotes_task_then_releases(self):
        mailbox, harness = self.harness()
        append(mailbox, pending('stale', 1))
        harness.pending_tasks['group:7'] = [{'kind': 'task', 'task_id': 'task-1'}]
        harness.commit_current_turn(outbound=True, turn_log=True, metadata=True)
        promoted = harness.handoff(
            CompletedTurn('group:7'), is_stale=lambda _item: True,
        )
        self.assertIsNone(promoted.followup)
        self.assertEqual(promoted.promoted_task['task_id'], 'task-1')
        self.assertFalse(promoted.scope_released)
        released = harness.handoff(
            CompletedTurn('group:7'), is_stale=lambda _item: False,
        )
        self.assertIsNone(released.followup)
        self.assertIsNone(released.promoted_task)
        self.assertTrue(released.scope_released)

    def test_tool_raw_pop_remains_outside_completed_turn_handoff(self):
        mailbox, harness = self.harness()
        stale = pending('tool-stale', 1)
        later = pending('later', 2)
        append(mailbox, stale)
        append(mailbox, later)
        self.assertIs(harness.coordinator.pop_tool_raw('group:7'), stale)
        self.assertEqual(mailbox.pending_count('group:7'), 1)
        harness.commit_current_turn(outbound=True, turn_log=True, metadata=True)
        result = harness.handoff(
            CompletedTurn('group:7'), is_stale=lambda _item: False,
        )
        self.assertEqual(result.followup['cleaned'], 'later')


if __name__ == '__main__':
    unittest.main()
