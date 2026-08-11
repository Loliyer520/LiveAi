import unittest

from core.event_envelope import EventEnvelope, EventType
from core.event_mailbox import EventBatch, InMemoryEventMailbox


def event(text, scope_type='group', scope_id='7', event_id=None):
    kwargs = {}
    if event_id is not None:
        kwargs['event_id'] = event_id
    return EventEnvelope(
        event_type=EventType.MESSAGE,
        scope_type=scope_type,
        scope_id=scope_id,
        payload={'text': text},
        source='characterization-test',
        occurred_at=100.0,
        **kwargs,
    )


class EventEnvelopeContractTests(unittest.TestCase):
    def test_round_trip_serialization_preserves_schema(self):
        original = event('hello', event_id='event-1')
        restored = EventEnvelope.from_dict(original.to_dict())
        self.assertEqual(restored, original)
        self.assertEqual(restored.scope_key, 'group:7')
        self.assertEqual(restored.event_type, EventType.MESSAGE)

    def test_validation_rejects_unknown_types_and_non_json_payload(self):
        with self.assertRaises(ValueError):
            EventEnvelope('unknown', 'group', '7', {}, 'test')
        with self.assertRaises(ValueError):
            EventEnvelope(EventType.MESSAGE, 'group', '7', {'bad': object()}, 'test')
        with self.assertRaises(ValueError):
            EventEnvelope.from_dict({'event_type': 'message'})


class InMemoryEventMailboxContractTests(unittest.TestCase):
    def test_same_scope_is_fifo_and_drain_is_one_batch(self):
        mailbox = InMemoryEventMailbox()
        first, second = mailbox.append_many((event('one'), event('two')))
        self.assertLess(first.mailbox_sequence, second.mailbox_sequence)
        batch = mailbox.drain_scope('group:7')
        self.assertIsInstance(batch, EventBatch)
        self.assertEqual([item.payload['text'] for item in batch.events], ['one', 'two'])
        self.assertEqual(batch.merged_payload()['event_count'], 2)
        self.assertTrue(mailbox.is_empty())

    def test_different_scopes_are_isolated(self):
        mailbox = InMemoryEventMailbox()
        mailbox.append(event('group-event'))
        mailbox.append(event('private-event', scope_type='private', scope_id='9'))
        self.assertEqual(mailbox.pending_scopes(), ('group:7', 'private:9'))
        group_batch = mailbox.drain_scope('group:7')
        self.assertEqual([item.payload['text'] for item in group_batch.events], ['group-event'])
        self.assertEqual(mailbox.pending_count('private:9'), 1)

    def test_events_appended_during_turn_are_drained_after_turn_as_snapshot(self):
        mailbox = InMemoryEventMailbox()
        mailbox.append(event('during-generation-1'))
        mailbox.append(event('during-generation-2'))
        batch = mailbox.drain_scope('group:7')
        mailbox.append(event('arrived-after-drain'))
        self.assertEqual(
            [item.payload['text'] for item in batch.events],
            ['during-generation-1', 'during-generation-2'],
        )
        self.assertEqual(mailbox.pending_count('group:7'), 1)

    def test_new_instance_does_not_restore_events(self):
        old_process = InMemoryEventMailbox()
        old_process.append(event('lost-on-restart'))
        new_process = InMemoryEventMailbox()
        self.assertTrue(new_process.is_empty())
        self.assertEqual(new_process.pending_scopes(), ())

    def test_sequence_orders_scopes_by_first_arrival_without_deduplication(self):
        mailbox = InMemoryEventMailbox()
        mailbox.append(event('private-first', 'private', '9', event_id='same-id'))
        mailbox.append(event('group-second', event_id='same-id'))
        mailbox.append(event('private-duplicate-id', 'private', '9', event_id='same-id'))
        self.assertEqual(mailbox.pending_scopes(), ('private:9', 'group:7'))
        private_batch = mailbox.drain_scope('private:9')
        self.assertEqual(len(private_batch.events), 2)
        self.assertEqual(
            [item.mailbox_sequence for item in private_batch.events],
            sorted(item.mailbox_sequence for item in private_batch.events),
        )

    def test_atomic_single_pop_preserves_remaining_fifo_and_clear(self):
        mailbox = InMemoryEventMailbox()
        mailbox.append_many((event('one'), event('two'), event('three')))
        self.assertEqual(mailbox.pop_scope('group:7').payload['text'], 'one')
        self.assertEqual(mailbox.pending_count('group:7'), 2)
        self.assertEqual(
            [item.payload['text'] for item in mailbox.drain_scope('group:7').events],
            ['two', 'three'],
        )
        mailbox.append(event('after'))
        mailbox.clear()
        self.assertTrue(mailbox.is_empty())


    def test_append_many_rejects_mid_batch_invalid_event_without_partial_commit(self):
        mailbox = InMemoryEventMailbox()
        with self.assertRaises(TypeError):
            mailbox.append_many((event('valid'), object(), event('never-committed')))
        self.assertEqual(mailbox.pending_count(), 0)
        queued = mailbox.append(event('after-failure'))
        self.assertEqual(queued.mailbox_sequence, 1)

    def test_concurrent_drain_cannot_observe_half_of_append_many(self):
        import threading

        mailbox = InMemoryEventMailbox()
        midpoint = threading.Event()
        resume = threading.Event()
        append_result = []
        drain_result = []

        def staged_events():
            yield event('one')
            midpoint.set()
            self.assertTrue(resume.wait(2))
            yield event('two')

        append_thread = threading.Thread(
            target=lambda: append_result.append(mailbox.append_many(staged_events()))
        )
        append_thread.start()
        self.assertTrue(midpoint.wait(2))

        drain_thread = threading.Thread(
            target=lambda: drain_result.append(mailbox.drain_scope('group:7'))
        )
        drain_thread.start()
        drain_thread.join(2)
        self.assertFalse(drain_thread.is_alive())
        self.assertEqual(drain_result, [None])

        resume.set()
        append_thread.join(2)
        self.assertFalse(append_thread.is_alive())
        self.assertEqual(len(append_result[0]), 2)
        committed = mailbox.drain_scope('group:7')
        self.assertEqual([item.payload['text'] for item in committed.events], ['one', 'two'])

    def test_partial_pop_reorders_pending_scopes_by_remaining_head(self):
        mailbox = InMemoryEventMailbox()
        mailbox.append(event('group-first'))
        mailbox.append(event('private-second', 'private', '9'))
        mailbox.append(event('group-third'))
        self.assertEqual(mailbox.pending_scopes(), ('group:7', 'private:9'))
        mailbox.pop_scope('group:7')
        self.assertEqual(mailbox.pending_scopes(), ('private:9', 'group:7'))

    def test_clear_empties_counts_and_keeps_sequence_monotonic(self):
        mailbox = InMemoryEventMailbox()
        mailbox.append_many((event('one'), event('two')))
        mailbox.clear()
        self.assertEqual(mailbox.pending_count(), 0)
        self.assertEqual(mailbox.pending_count('group:7'), 0)
        self.assertEqual(mailbox.pending_scopes(), ())
        self.assertTrue(mailbox.is_empty())
        self.assertEqual(mailbox.append(event('after-clear')).mailbox_sequence, 3)

    def test_single_and_fifo_transient_identity_is_preserved(self):
        mailbox = InMemoryEventMailbox()
        first = {'value': 1}
        second = ['value']
        mailbox.append(event('one'), transient=first)
        mailbox.append(event('two'), transient=second)

        popped = mailbox.pop_scope_entry('group:7')
        self.assertIs(popped.transient, first)
        batch = mailbox.drain_scope('group:7')
        self.assertIs(batch.transients[0], second)
        self.assertIs(batch.entries[0].transient, second)

    def test_transient_mutation_remains_visible_and_is_not_serialized(self):
        import json

        mailbox = InMemoryEventMailbox()
        sentinel = object()
        transient = {'state': 'queued', 'sentinel': sentinel}
        mailbox.append(event('identity'), transient=transient)
        transient['state'] = 'changed-after-enqueue'

        batch = mailbox.drain_scope('group:7')
        self.assertIs(batch.transients[0], transient)
        self.assertEqual(batch.transients[0]['state'], 'changed-after-enqueue')
        self.assertIs(batch.transients[0]['sentinel'], sentinel)
        serialized = batch.merged_payload()
        json.dumps(serialized)
        self.assertNotIn('transient', repr(serialized).lower())

if __name__ == '__main__':
    unittest.main()
