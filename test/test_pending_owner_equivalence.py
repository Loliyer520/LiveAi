import json
import unittest
from types import SimpleNamespace

from core.ai_runtime import AIOrchestrator
from core.event_adapters import (
    envelope_from_scope_turn_item,
    scope_turn_item_from_batch,
    scope_turn_item_from_envelope,
)
from core.event_batch_coordinator import AtomicTurnBatchCoordinator
from core.event_mailbox import InMemoryEventMailbox
from core.character_session import CharacterSessionRegistry
from core.events import ChatMessage


def project_pending_task_owner(owner):
    """Return an immutable, non-aliasing view of the legacy pending-task owner."""
    return tuple(
        (
            scope_key,
            tuple(
                (
                    id(entry),
                    entry.get('kind'),
                    entry.get('task_id'),
                    entry.get('message_epoch'),
                    entry.get('scope_prereserved'),
                )
                for entry in queue
            ),
        )
        for scope_key, queue in sorted(owner.items())
    )


class PendingOwnerEquivalenceTests(unittest.TestCase):
    def message_item(self, text, *, chat_id=100, user_id=1, message_id=None,
                     timestamp=10, **extra):
        item = {
            'kind': 'message',
            'message': ChatMessage(
                'group', chat_id, user_id, text, raw_message=text,
                sender={'nickname': f'u{user_id}'}, message_id=message_id,
                timestamp=timestamp, raw_data={'source': 'test'},
            ),
            'cleaned': text,
            'agent_id': 'a',
            'deferred_count': 1,
            'trigger_messages': [{'text': text}],
            'message_epoch': 4,
            'history_seed': None,
            'metadata': {'text': text},
        }
        item.update(extra)
        return item

    def runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._event_mailbox = InMemoryEventMailbox()
        runtime._turn_batch_coordinator = AtomicTurnBatchCoordinator(runtime._event_mailbox)
        runtime._character_sessions = CharacterSessionRegistry(mailbox=runtime._event_mailbox)
        runtime._observed_pending_tasks = {}
        append_pending_task = runtime._character_sessions.append_pending_task
        promote_pending_task = (
            runtime._character_sessions.promote_pending_task_if_mailbox_empty
        )
        clear_pending_tasks = runtime._character_sessions.clear_pending_tasks

        def observed_append(scope_key, task):
            index = append_pending_task(scope_key, task)
            runtime._observed_pending_tasks.setdefault(scope_key, []).append(task)
            return index

        def observed_promote(scope_key):
            task = promote_pending_task(scope_key)
            if task is not None:
                pending = runtime._observed_pending_tasks[scope_key]
                self.assertIs(pending.pop(0), task)
                if not pending:
                    runtime._observed_pending_tasks.pop(scope_key)
            return task

        def observed_clear(scope_key=None):
            clear_pending_tasks(scope_key)
            if scope_key is None:
                runtime._observed_pending_tasks.clear()
            else:
                runtime._observed_pending_tasks.pop(scope_key, None)

        runtime._character_sessions.append_pending_task = observed_append
        runtime._character_sessions.promote_pending_task_if_mailbox_empty = observed_promote
        runtime._character_sessions.clear_pending_tasks = observed_clear
        runtime._message_epoch = 4
        runtime._is_message_stale = lambda message: False
        runtime._is_epoch_stale = lambda epoch: epoch != runtime._message_epoch
        runtime.queue = SimpleNamespace(items=[], put_nowait=lambda item: runtime.queue.items.append(item))
        return runtime

    def test_raw_pop_fifo_count_complete_shape_and_scope_clear_equivalence(self):
        runtime = self.runtime()
        mailbox = InMemoryEventMailbox()
        first = self.message_item('first', message_id='001')
        second = self.message_item('second', user_id=2, message_id='002')
        other = self.message_item('other', chat_id=200, message_id='003')

        for item in (first, second):
            runtime._append_pending_scope_turn('group:100', item)
            mailbox.append_entry(envelope_from_scope_turn_item(item), transient=item)
        runtime._append_pending_scope_turn('group:200', other)
        mailbox.append_entry(envelope_from_scope_turn_item(other), transient=other)

        self.assertEqual(runtime._pending_scope_turn_count('group:100'), 2)
        self.assertEqual(mailbox.pending_count('group:100'), 2)
        raw_runtime = runtime._pop_pending_scope_turn('group:100')
        raw_mailbox = mailbox.pop_scope_entry('group:100')
        self.assertIs(raw_runtime, first)
        self.assertIs(raw_mailbox.transient, first)
        self.assertEqual(runtime._pending_scope_turn_count('group:100'), 1)
        self.assertEqual(mailbox.pending_count('group:100'), 1)
        restored = scope_turn_item_from_envelope(raw_mailbox.envelope)
        self.assertEqual(
            set(restored),
            {'kind', 'message', 'cleaned', 'agent_id', 'deferred_count',
             'trigger_messages', 'message_epoch', 'history_seed', 'metadata',
             'silent_event', 'scope_key', 'mailbox_event_ids', 'mailbox_sequences'},
        )
        self.assertEqual(restored['message'].text, raw_runtime['message'].text)

        while runtime._pop_pending_scope_turn('group:100') is not None:
            pass
        mailbox.drain_scope('group:100')
        self.assertEqual(runtime._pending_scope_turn_count('group:100'), 0)
        self.assertEqual(mailbox.pending_count('group:100'), 0)
        self.assertEqual(runtime._pending_scope_turn_count('group:200'), 1)
        self.assertEqual(mailbox.pending_count('group:200'), 1)

    def test_live_pop_filters_stale_but_raw_pop_does_not(self):
        stale = self.message_item('stale', message_id='stale')
        live = self.message_item('live', message_id='live')

        raw_runtime = self.runtime()
        raw_runtime._is_message_stale = lambda message: message.message_id == 'stale'
        raw_runtime._append_pending_scope_turn('group:100', stale)
        raw_runtime._append_pending_scope_turn('group:100', live)
        self.assertIs(raw_runtime._pop_pending_scope_turn('group:100'), stale)

        filtered_runtime = self.runtime()
        filtered_runtime._is_message_stale = lambda message: message.message_id == 'stale'
        filtered_runtime._append_pending_scope_turn('group:100', stale)
        filtered_runtime._append_pending_scope_turn('group:100', live)
        self.assertIs(filtered_runtime._pop_next_live_pending_scope_turn('group:100'), live)
        self.assertEqual(filtered_runtime._pending_scope_turn_count('group:100'), 0)

        mailbox = InMemoryEventMailbox()
        mailbox.append_many(envelope_from_scope_turn_item(item) for item in (stale, live))
        self.assertEqual(
            mailbox.pop_scope('group:100').event_id,
            envelope_from_scope_turn_item(stale).event_id,
        )

    def test_release_prefers_message_with_history_seed_then_promotes_task(self):
        runtime = self.runtime()
        scope_key = 'group:100'
        runtime._activate_scope_turn(scope_key)
        pending_message = self.message_item('pending', history_seed=None)
        runtime._append_pending_scope_turn(scope_key, pending_message)
        runtime._character_sessions.append_pending_task(
            scope_key,
            {'kind': 'task', 'task_id': 'task-1', 'message_epoch': 4},
        )
        active = {'scope_key': scope_key, 'followup_history_seed': [
            {'role': 'assistant', 'content': 'previous answer'},
        ]}

        followup = runtime._release_scope_turn(active)
        self.assertIs(followup, pending_message)
        self.assertEqual(followup['history_seed'], active['followup_history_seed'])
        self.assertEqual(runtime.queue.items, [])
        self.assertTrue(runtime._character_sessions.is_active(scope_key))

        self.assertIsNone(runtime._release_scope_turn({'scope_key': scope_key}))
        self.assertEqual(runtime.queue.items[0]['task_id'], 'task-1')
        self.assertTrue(runtime.queue.items[0]['scope_prereserved'])
        self.assertTrue(runtime._character_sessions.is_active(scope_key))

    def test_incomplete_turn_followup_still_batches_all_mailbox_events(self):
        runtime = self.runtime()
        first = self.message_item('second', message_id='002', metadata={'slot': 2})
        second = self.message_item('third', message_id='003', metadata={'slot': 3})
        runtime._append_pending_scope_turn('group:100', first)
        runtime._append_pending_scope_turn('group:100', second)

        followup = runtime._merge_followup_after_turn(
            {
                'scope_key': 'group:100',
                'followup_history_seed': [{'role': 'assistant', 'content': 'seed'}],
            },
            False,
        )

        self.assertEqual(followup['history_seed'], [{'role': 'assistant', 'content': 'seed'}])
        self.assertEqual([entry['text'] for entry in followup['trigger_messages']], ['second', 'third'])
        self.assertEqual([item['metadata']['slot'] for item in followup['batch_items']], [2, 3])
        self.assertEqual(followup['mailbox_sequences'], sorted(followup['mailbox_sequences']))
        self.assertEqual(runtime._pending_scope_turn_count('group:100'), 0)

    def test_live_tool_drain_uses_same_batch_merge_contract_as_turn_handoff(self):
        runtime = self.runtime()
        first = self.message_item('first', message_id='001', metadata={'slot': 1})
        second = self.message_item('second', message_id='002', metadata={'slot': 2})
        runtime._append_pending_scope_turn('group:100', first)
        runtime._append_pending_scope_turn('group:100', second)

        merged = runtime._drain_live_tool_scope_turn('group:100')

        self.assertEqual(merged['message'].text, 'second')
        self.assertEqual([entry['text'] for entry in merged['trigger_messages']], ['first', 'second'])
        self.assertEqual([item['metadata']['slot'] for item in merged['batch_items']], [1, 2])
        self.assertEqual(merged['mailbox_event_ids'], ['001', '002'])
        self.assertEqual(runtime._pending_scope_turn_count('group:100'), 0)

    def test_live_tool_drain_keeps_silent_items_when_batch_contains_visible_event(self):
        runtime = self.runtime()
        first = self.message_item(
            'self-sent',
            message_id='001',
            metadata={'slot': 1},
            silent_event=True,
        )
        second = self.message_item('visible', message_id='002', metadata={'slot': 2})
        runtime._append_pending_scope_turn('group:100', first)
        runtime._append_pending_scope_turn('group:100', second)

        merged = runtime._drain_live_tool_scope_turn('group:100')

        self.assertEqual([entry['text'] for entry in merged['trigger_messages']], ['self-sent', 'visible'])
        self.assertEqual(
            [bool(item.get('silent_event')) for item in merged['batch_items']],
            [True, False],
        )
        self.assertEqual(runtime._pending_scope_turn_count('group:100'), 0)

    def test_silent_only_followup_batch_is_consumed_without_new_turn(self):
        runtime = self.runtime()
        runtime._append_pending_scope_turn(
            'group:100',
            self.message_item('self-sent', message_id='001', silent_event=True),
        )

        followup = runtime._merge_followup_after_turn({'scope_key': 'group:100'}, False)

        self.assertIsNone(followup)
        self.assertEqual(runtime._pending_scope_turn_count('group:100'), 0)

    def test_agent_report_flush_delivers_active_urgent_only_and_requeues_normal(self):
        runtime = self.runtime()
        runtime._character_sessions.activate('group:7')
        reports = [
            {'agent_id': 'normal', 'text': 'later', 'origin_scope': 'group:7'},
            {'agent_id': 'urgent', 'text': 'now', 'origin_scope': 'group:7', 'urgent': True},
            {'agent_id': 'idle', 'text': 'deliver', 'origin_scope': 'group:8'},
        ]

        class Manager:
            def __init__(self, items):
                self.items = list(items)
                self.requeued = []

            def has_pending_reports(self):
                return bool(self.items)

            def drain_pending_reports(self):
                items, self.items = self.items, []
                return items

            def requeue_pending_reports(self, items):
                self.requeued.extend(items)

        runtime.agent_manager = Manager(reports)
        delivered = []
        runtime._deliver_agent_reports_to_scope = (
            lambda scope_type, scope_id, items: delivered.append(
                (f'{scope_type}:{scope_id}', [item['agent_id'] for item in items])
            )
        )
        runtime._flush_agent_reports(only_if_idle=True)
        self.assertEqual(delivered, [('group:7', ['urgent']), ('group:8', ['idle'])])
        self.assertEqual([item['agent_id'] for item in runtime.agent_manager.requeued], ['normal'])

    def test_clear_reset_removes_active_pending_and_status_count(self):
        runtime = self.runtime()
        runtime._activate_scope_turn('group:100')
        runtime._append_pending_scope_turn('group:100', self.message_item('one'))
        runtime._append_pending_scope_turn('group:100', self.message_item('two'))
        runtime._activate_scope_turn('group:200')
        self.assertEqual(runtime._pending_scope_turn_count('group:100'), 2)
        self.assertTrue(runtime._scope_turn_is_busy('group:100'))

        runtime._clear_scope_turn_coordination()
        self.assertEqual(runtime._pending_scope_turn_count('group:100'), 0)
        self.assertFalse(runtime._scope_turn_is_busy('group:100'))
        self.assertFalse(runtime._scope_turn_is_active('group:200'))
        self.assertEqual(runtime._pending_scope_turn_count('group:200'), 0)
        self.assertFalse(any(snapshot.active for snapshot in runtime._character_sessions.snapshots()))

    def test_atomic_batch_drain_leaves_new_events_for_next_batch(self):
        mailbox = InMemoryEventMailbox()
        first = self.message_item('first', message_id=1)
        second = self.message_item('second', message_id=2)
        later = self.message_item('later', message_id=3)
        mailbox.append_many(envelope_from_scope_turn_item(item) for item in (first, second))

        batch = mailbox.drain_scope('group:100')
        mailbox.append(envelope_from_scope_turn_item(later))
        self.assertEqual([event.event_id for event in batch.events], ['1', '2'])
        self.assertEqual(mailbox.pending_count('group:100'), 1)
        next_batch = mailbox.drain_scope('group:100')
        self.assertEqual([event.event_id for event in next_batch.events], ['3'])
        self.assertLess(batch.last_sequence, next_batch.first_sequence)

    def test_fifo_batch_merge_uses_last_message_metadata_and_first_history_seed(self):
        mailbox = InMemoryEventMailbox()
        legacy_items = [
            self.message_item(
                'first', message_id='001',
                history_seed=[{'role': 'user', 'content': 'seed'}],
                metadata={'slot': 1},
            ),
            self.message_item(
                'second', user_id=2, message_id='', timestamp='',
                history_seed=None, metadata={'slot': 2},
            ),
        ]
        mailbox.append_many(envelope_from_scope_turn_item(item) for item in legacy_items)
        merged = scope_turn_item_from_batch(mailbox.drain_scope('group:100'))
        self.assertEqual(merged['message'].text, 'second')
        self.assertEqual(merged['metadata'], {'slot': 2})
        self.assertEqual([entry['text'] for entry in merged['trigger_messages']], ['first', 'second'])
        self.assertEqual(merged['message_epoch'], 4)
        self.assertEqual(merged['history_seed'], legacy_items[0]['history_seed'])
        self.assertEqual([item['metadata']['slot'] for item in merged['batch_items']], [1, 2])
        self.assertEqual(merged['mailbox_sequences'], sorted(merged['mailbox_sequences']))

    def test_single_and_multiple_entry_transients_preserve_identity_and_mutation(self):
        mailbox = InMemoryEventMailbox()
        first = self.message_item('first', message_id=1)
        second = self.message_item('second', message_id=2)
        stored_first = mailbox.append_entry(envelope_from_scope_turn_item(first), transient=first)
        stored_second = mailbox.append_entry(envelope_from_scope_turn_item(second), transient=second)
        first['metadata']['mutated_after_enqueue'] = True

        popped = mailbox.pop_scope_entry('group:100')
        self.assertIs(popped, stored_first)
        self.assertIs(popped.transient, first)
        self.assertTrue(popped.transient['metadata']['mutated_after_enqueue'])
        drained = mailbox.drain_scope('group:100').entries
        self.assertEqual(len(drained), 1)
        self.assertEqual(
            drained[0].envelope.mailbox_sequence,
            stored_second.envelope.mailbox_sequence,
        )
        self.assertIs(drained[0].envelope, stored_second.envelope)
        self.assertIs(drained[0].transient, second)

    def test_pop_add_history_seed_and_requeue_keeps_same_object(self):
        mailbox = InMemoryEventMailbox()
        item = self.message_item('retry me', message_id=9, history_seed=None)
        mailbox.append_entry(envelope_from_scope_turn_item(item), transient=item)
        popped = mailbox.pop_scope_entry('group:100')
        popped.transient['history_seed'] = [{'role': 'assistant', 'content': 'seed after pop'}]
        requeued = mailbox.append_entry(
            envelope_from_scope_turn_item(popped.transient), transient=popped.transient,
        )
        again = mailbox.pop_scope_entry('group:100')
        self.assertIs(again.transient, item)
        self.assertIs(requeued.transient, item)
        self.assertEqual(again.transient['history_seed'][0]['content'], 'seed after pop')
        self.assertEqual(
            scope_turn_item_from_envelope(again.envelope)['history_seed'],
            item['history_seed'],
        )

    def test_non_json_transient_sentinel_never_leaks_to_envelope_or_to_dict(self):
        mailbox = InMemoryEventMailbox()
        sentinel = object()
        item = self.message_item('sentinel', message_id=10)
        transient = {'item': item, 'non_json': sentinel}
        entry = mailbox.append_entry(envelope_from_scope_turn_item(item), transient=transient)
        popped = mailbox.pop_scope_entry('group:100')
        self.assertIs(popped.transient['non_json'], sentinel)
        self.assertNotIn('transient', popped.envelope.payload)
        self.assertNotIn('non_json', repr(popped.envelope.to_dict()))
        json.dumps(popped.envelope.to_dict())
        self.assertIs(entry.transient, transient)

    def test_batch_transients_align_with_events_and_sequences(self):
        mailbox = InMemoryEventMailbox()
        items = [self.message_item(str(index), message_id=index) for index in (1, 2, 3)]
        entries = [
            mailbox.append_entry(envelope_from_scope_turn_item(item), transient=item)
            for item in items
        ]
        drained_batch = mailbox.drain_scope('group:100')
        drained = drained_batch.entries
        batch = drained_batch
        self.assertEqual(
            tuple(entry.envelope.mailbox_sequence for entry in drained),
            tuple(entry.envelope.mailbox_sequence for entry in entries),
        )
        self.assertEqual(batch.events, tuple(entry.envelope for entry in entries))
        self.assertEqual(
            [event.mailbox_sequence for event in batch.events],
            [entry.envelope.mailbox_sequence for entry in entries],
        )
        self.assertEqual(batch.first_sequence, entries[0].envelope.mailbox_sequence)
        self.assertEqual(batch.last_sequence, entries[-1].envelope.mailbox_sequence)
        for entry, item, event in zip(drained, items, batch.events):
            self.assertIs(entry.transient, item)
            self.assertIs(entry.envelope, event)

    def test_task_promotion_reenvelopes_after_current_fifo_batch(self):
        mailbox = InMemoryEventMailbox()
        first = self.message_item('one', chat_id=8, message_id=1, metadata={'order': 1})
        promoted_task = {
            'kind': 'task', 'scope_key': 'group:8', 'task_id': 'task-2',
            'text': 'promoted', 'metadata': {'order': 2},
        }
        report = {
            'kind': 'report', 'scope_key': 'group:8', 'report_id': 'report-3',
            'text': 'report', 'metadata': {'order': 3},
        }
        mailbox.append_many(envelope_from_scope_turn_item(item) for item in (first, promoted_task, report))
        initial = scope_turn_item_from_batch(mailbox.drain_scope('group:8'))
        self.assertEqual([item['metadata']['order'] for item in initial['batch_items']], [1, 2, 3])

        promoted = initial['batch_items'][1]
        promoted['metadata']['promotion'] = {'attempt': 1}
        reenveloped = mailbox.append(envelope_from_scope_turn_item(promoted))
        restored = scope_turn_item_from_envelope(mailbox.drain_scope('group:8').events[0])
        self.assertEqual(restored['kind'], 'task')
        self.assertEqual(restored['metadata']['order'], 2)
        self.assertEqual(restored['metadata']['promotion'], {'attempt': 1})
        self.assertGreater(reenveloped.mailbox_sequence, initial['mailbox_sequences'][-1])
        self.assertNotIn('mailbox_event_ids', reenveloped.payload['item'])
        self.assertNotIn('mailbox_sequences', reenveloped.payload['item'])



    def test_task_release_promotes_pending_fifo_with_same_entry_identity(self):
        runtime = self.runtime()
        scope = 'group:100'
        task_a = {'kind': 'task', 'task_id': 'task-a', 'message_epoch': 4}
        task_b = {'kind': 'task', 'task_id': 'task-b', 'message_epoch': 4}
        task_c = {'kind': 'task', 'task_id': 'task-c', 'message_epoch': 4}

        self.assertFalse(runtime._scope_turn_is_active(scope))
        self.assertTrue(runtime._reserve_task_scope(scope, task_a))
        self.assertTrue(runtime._scope_turn_is_active(scope))
        self.assertEqual(runtime._observed_pending_tasks, {})
        self.assertEqual(runtime.queue.items, [])

        self.assertFalse(runtime._reserve_task_scope(scope, task_b))
        self.assertFalse(runtime._reserve_task_scope(scope, task_c))
        stored_b, stored_c = runtime._observed_pending_tasks[scope]
        self.assertIsNot(stored_b, task_b)
        self.assertIsNot(stored_c, task_c)
        self.assertEqual(stored_b, {
            'kind': 'task', 'task_id': 'task-b', 'message_epoch': 4,
        })
        self.assertEqual(stored_c, {
            'kind': 'task', 'task_id': 'task-c', 'message_epoch': 4,
        })
        projection = project_pending_task_owner(runtime._observed_pending_tasks)
        self.assertEqual(
            projection,
            ((scope, (
                (id(stored_b), 'task', 'task-b', 4, None),
                (id(stored_c), 'task', 'task-c', 4, None),
            )),),
        )

        runtime._release_task_scope(scope)
        promoted_b = runtime.queue.items.pop(0)
        self.assertIs(promoted_b, stored_b)
        self.assertTrue(promoted_b['scope_prereserved'])
        self.assertTrue(runtime._scope_turn_is_active(scope))
        self.assertEqual(runtime._observed_pending_tasks[scope], [stored_c])
        self.assertIsNone(projection[0][1][0][4])

        runtime._release_task_scope(scope)
        promoted_c = runtime.queue.items.pop(0)
        self.assertIs(promoted_c, stored_c)
        self.assertTrue(promoted_c['scope_prereserved'])
        self.assertTrue(runtime._scope_turn_is_active(scope))
        self.assertNotIn(scope, runtime._observed_pending_tasks)

        runtime._release_task_scope(scope)
        self.assertFalse(runtime._scope_turn_is_active(scope))
        self.assertEqual(runtime._observed_pending_tasks, {})
        self.assertEqual(runtime.queue.items, [])

    def test_task_release_skips_stale_fifo_and_deactivates_when_all_stale(self):
        runtime = self.runtime()
        runtime._is_epoch_stale = AIOrchestrator._is_epoch_stale.__get__(
            runtime, AIOrchestrator
        )
        scope = 'group:100'
        self.assertTrue(runtime._reserve_task_scope(scope, {
            'kind': 'task', 'task_id': 'task-a', 'message_epoch': 4,
        }))
        for task_id, epoch in (
            ('stale-1', 2),
            ('stale-2', 3),
            ('live-3', 4),
            ('live-4', 4),
        ):
            self.assertFalse(runtime._reserve_task_scope(scope, {
                'kind': 'task', 'task_id': task_id, 'message_epoch': epoch,
            }))
        stored_stale_1, stored_stale_2, stored_live_3, stored_live_4 = (
            runtime._observed_pending_tasks[scope]
        )
        projection = project_pending_task_owner(runtime._observed_pending_tasks)
        self.assertEqual(
            tuple(entry[2] for entry in projection[0][1]),
            ('stale-1', 'stale-2', 'live-3', 'live-4'),
        )

        runtime._release_task_scope(scope)
        promoted_live_3 = runtime.queue.items.pop(0)
        self.assertIs(promoted_live_3, stored_live_3)
        self.assertTrue(promoted_live_3['scope_prereserved'])
        self.assertNotIn('scope_prereserved', stored_stale_1)
        self.assertNotIn('scope_prereserved', stored_stale_2)
        self.assertEqual(runtime._observed_pending_tasks[scope], [stored_live_4])
        self.assertNotIn('scope_prereserved', stored_live_4)
        self.assertTrue(runtime._scope_turn_is_active(scope))

        runtime._release_task_scope(scope)
        promoted_live_4 = runtime.queue.items.pop(0)
        self.assertIs(promoted_live_4, stored_live_4)
        self.assertTrue(promoted_live_4['scope_prereserved'])
        self.assertNotIn(scope, runtime._observed_pending_tasks)
        self.assertTrue(runtime._scope_turn_is_active(scope))

        runtime._release_task_scope(scope)
        self.assertFalse(runtime._scope_turn_is_active(scope))
        self.assertEqual(runtime.queue.items, [])

        all_stale_runtime = self.runtime()
        all_stale_runtime._is_epoch_stale = (
            AIOrchestrator._is_epoch_stale.__get__(
                all_stale_runtime, AIOrchestrator
            )
        )
        all_stale_scope = 'group:all-stale'
        self.assertTrue(all_stale_runtime._reserve_task_scope(
            all_stale_scope,
            {'kind': 'task', 'task_id': 'active', 'message_epoch': 4},
        ))
        for task_id, epoch in (('stale-1', 1), ('stale-2', 3)):
            self.assertFalse(all_stale_runtime._reserve_task_scope(
                all_stale_scope,
                {'kind': 'task', 'task_id': task_id, 'message_epoch': epoch},
            ))
        all_stale_1, all_stale_2 = (
            all_stale_runtime._observed_pending_tasks[all_stale_scope]
        )
        all_stale_runtime._release_task_scope(all_stale_scope)
        self.assertEqual(all_stale_runtime.queue.items, [])
        self.assertNotIn(
            all_stale_scope, all_stale_runtime._observed_pending_tasks
        )
        self.assertFalse(
            all_stale_runtime._scope_turn_is_active(all_stale_scope)
        )
        self.assertNotIn('scope_prereserved', all_stale_1)
        self.assertNotIn('scope_prereserved', all_stale_2)

    def test_message_release_defers_same_pending_task_until_next_handoff(self):
        runtime = self.runtime()
        scope = 'group:100'
        active_message = self.message_item('active')
        pending_message = self.message_item('message-first')
        task = {
            'kind': 'task', 'task_id': 'task-after', 'message_epoch': 4,
        }

        self.assertFalse(runtime._scope_turn_is_active(scope))
        self.assertTrue(runtime._reserve_scope_turn(active_message))
        self.assertTrue(runtime._scope_turn_is_active(scope))
        self.assertFalse(runtime._reserve_task_scope(scope, task))
        stored_task = runtime._observed_pending_tasks[scope][0]
        self.assertIsNot(stored_task, task)
        self.assertEqual(stored_task, {
            'kind': 'task', 'task_id': 'task-after', 'message_epoch': 4,
        })
        projection = project_pending_task_owner(runtime._observed_pending_tasks)

        self.assertFalse(runtime._reserve_scope_turn(pending_message))
        self.assertEqual(runtime._pending_scope_turn_count(scope), 1)
        self.assertIs(runtime._observed_pending_tasks[scope][0], stored_task)
        self.assertEqual(runtime.queue.items, [])

        followup = runtime._release_scope_turn(active_message)
        self.assertIsNotNone(followup)
        self.assertEqual(followup['cleaned'], pending_message['cleaned'])
        self.assertEqual(runtime._pending_scope_turn_count(scope), 0)
        self.assertEqual(runtime.queue.items, [])
        self.assertIs(runtime._observed_pending_tasks[scope][0], stored_task)
        self.assertNotIn('scope_prereserved', stored_task)
        self.assertTrue(runtime._scope_turn_is_active(scope))
        self.assertEqual(
            project_pending_task_owner(runtime._observed_pending_tasks),
            projection,
        )

        self.assertIsNone(runtime._release_scope_turn(followup))
        promoted = runtime.queue.items.pop(0)
        self.assertIs(promoted, stored_task)
        self.assertTrue(promoted['scope_prereserved'])
        self.assertNotIn(scope, runtime._observed_pending_tasks)
        self.assertTrue(runtime._scope_turn_is_active(scope))
        self.assertIsNone(projection[0][1][0][4])

        runtime._release_task_scope(scope)
        self.assertFalse(runtime._scope_turn_is_active(scope))
        self.assertEqual(runtime._observed_pending_tasks, {})
        self.assertEqual(runtime.queue.items, [])
if __name__ == '__main__':
    unittest.main()
