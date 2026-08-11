import unittest

from core.event_adapters import envelope_from_scope_turn_item
from core.event_batch_coordinator import AtomicTurnBatchCoordinator, CompletedTurn
from core.event_mailbox import InMemoryEventMailbox
from core.events import ChatMessage


def turn(text, message_id, *, stale=False, history_seed=None, metadata=None):
    return {
        'kind': 'message',
        'message': ChatMessage(
            chat_type='group', chat_id=7, user_id=message_id, text=text,
            raw_message=text, sender={'nickname': text}, message_id=message_id,
            timestamp=float(message_id), raw_data={'stale': stale},
        ),
        'cleaned': text,
        'agent_id': 'agent-1',
        'scope_key': 'group:7',
        'deferred_count': 1,
        'trigger_messages': [{'text': text, 'message_id': message_id}],
        'history_seed': history_seed,
        'metadata': metadata,
    }


def append(mailbox, item):
    mailbox.append(envelope_from_scope_turn_item(item), transient=item)


class AtomicTurnBatchCoordinatorTests(unittest.TestCase):
    def test_requires_completed_turn_proof_before_drain(self):
        mailbox = InMemoryEventMailbox()
        original = turn('pending', 1)
        append(mailbox, original)
        coordinator = AtomicTurnBatchCoordinator(mailbox)
        with self.assertRaises(TypeError):
            coordinator.drain_after_completed_turn('group:7', is_stale=lambda _item: False)
        self.assertEqual(mailbox.pending_count('group:7'), 1)

    def test_atomic_snapshot_leaves_later_event_for_next_batch(self):
        mailbox = InMemoryEventMailbox()
        first, second, later = turn('one', 1), turn('two', 2), turn('later', 3)
        append(mailbox, first)
        append(mailbox, second)
        coordinator = AtomicTurnBatchCoordinator(mailbox)

        def stale_with_late_arrival(item):
            if item is first:
                append(mailbox, later)
            return False

        batch = coordinator.drain_after_completed_turn(
            CompletedTurn('group:7'), is_stale=stale_with_late_arrival,
        )
        self.assertEqual(batch.entries[0].transient, first)
        self.assertEqual(batch.entries[1].transient, second)
        self.assertEqual(mailbox.pending_count('group:7'), 1)
        self.assertIs(coordinator.pop_tool_raw('group:7'), later)

    def test_fifo_identity_representative_trigger_history_and_metadata_rules(self):
        mailbox = InMemoryEventMailbox()
        first = turn('one', 1, history_seed=[{'role': 'old'}], metadata={'source': 'first'})
        second = turn('two', 2, metadata={'source': 'second'})
        append(mailbox, first)
        append(mailbox, second)
        completed = CompletedTurn(
            'group:7',
            history_seed=({'role': 'assistant', 'content': 'committed'},),
            metadata={'turn_id': 'turn-1', 'tool_results_committed': True},
        )
        batch = AtomicTurnBatchCoordinator(mailbox).drain_after_completed_turn(
            completed, is_stale=lambda _item: False,
        )
        self.assertIs(batch.entries[0].transient, first)
        self.assertIs(batch.entries[1].transient, second)
        self.assertIs(batch.representative, second)
        self.assertIs(batch.turn_item['message'], second['message'])
        self.assertEqual(
            [entry['text'] for entry in batch.turn_item['trigger_messages']],
            ['one', 'two'],
        )
        self.assertEqual(
            batch.turn_item['history_seed'],
            [{'role': 'assistant', 'content': 'committed'}],
        )
        self.assertEqual(batch.turn_item['turn_metadata']['turn_id'], 'turn-1')
        self.assertEqual(batch.turn_item['batch_metadata']['event_count'], 2)
        self.assertEqual(
            batch.turn_item['batch_metadata']['sequences'],
            sorted(batch.turn_item['batch_metadata']['sequences']),
        )

    def test_stale_filter_applies_only_to_normal_followup(self):
        mailbox = InMemoryEventMailbox()
        stale = turn('stale', 1, stale=True)
        live = turn('live', 2)
        append(mailbox, stale)
        append(mailbox, live)
        batch = AtomicTurnBatchCoordinator(mailbox).drain_after_completed_turn(
            CompletedTurn('group:7'),
            is_stale=lambda item: bool(item['message'].raw_data.get('stale')),
        )
        self.assertEqual(len(batch.entries), 1)
        self.assertIs(batch.representative, live)
        self.assertEqual(batch.turn_item['trigger_messages'], live['trigger_messages'])

    def test_tool_raw_pop_is_single_fifo_identity_without_stale_filter(self):
        mailbox = InMemoryEventMailbox()
        stale, live = turn('stale', 1, stale=True), turn('live', 2)
        append(mailbox, stale)
        append(mailbox, live)
        coordinator = AtomicTurnBatchCoordinator(mailbox)
        self.assertIs(coordinator.pop_tool_raw('group:7'), stale)
        self.assertEqual(mailbox.pending_count('group:7'), 1)
        self.assertIs(coordinator.pop_tool_raw('group:7'), live)

    def test_all_stale_snapshot_is_consumed_without_followup(self):
        mailbox = InMemoryEventMailbox()
        append(mailbox, turn('old-one', 1, stale=True))
        append(mailbox, turn('old-two', 2, stale=True))
        batch = AtomicTurnBatchCoordinator(mailbox).drain_after_completed_turn(
            CompletedTurn('group:7'), is_stale=lambda _item: True,
        )
        self.assertIsNone(batch)
        self.assertEqual(mailbox.pending_count('group:7'), 0)


if __name__ == '__main__':
    unittest.main()
