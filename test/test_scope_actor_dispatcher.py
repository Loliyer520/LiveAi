import asyncio
import unittest

from core.ai_runtime import AIOrchestrator
from core.character_session import CharacterSessionRegistry
from core.event_adapters import envelope_from_scope_turn_item
from core.event_batch_coordinator import AtomicTurnBatchCoordinator
from core.event_envelope import EventEnvelope, EventType
from core.event_mailbox import InMemoryEventMailbox
from core.events import ChatMessage
from core.scope_actor_dispatcher import ScopeActorDispatcher


def event(scope_type, scope_id, event_id):
    return EventEnvelope(
        event_type=EventType.MESSAGE,
        scope_type=scope_type,
        scope_id=scope_id,
        payload={'event_id': event_id},
        source='test',
        event_id=event_id,
        occurred_at=1,
    )


class ScopeActorDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mailbox = InMemoryEventMailbox()
        self.sessions = CharacterSessionRegistry(mailbox=self.mailbox)
        self.dispatcher = None

    async def asyncTearDown(self):
        if self.dispatcher is not None:
            await self.dispatcher.close()

    async def test_same_scope_is_serial_and_different_scopes_run_concurrently(self):
        first_started = asyncio.Event()
        other_started = asyncio.Event()
        release_first = asyncio.Event()
        order = []

        async def consume(scope_key, item):
            order.append(('start', scope_key, item['id']))
            if item['id'] == 'first':
                first_started.set()
                await release_first.wait()
            if item['id'] == 'other':
                other_started.set()
            order.append(('end', scope_key, item['id']))

        self.dispatcher = ScopeActorDispatcher(
            mailbox=self.mailbox,
            sessions=self.sessions,
            consume=consume,
        )
        self.dispatcher.submit_event(event('group', '1', 'first'), {'id': 'first'})
        self.dispatcher.submit_event(event('group', '1', 'second'), {'id': 'second'})
        self.dispatcher.submit_event(event('private', '2', 'other'), {'id': 'other'})

        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.wait_for(other_started.wait(), 1)
        self.assertNotIn(('start', 'group:1', 'second'), order)

        release_first.set()
        await asyncio.sleep(0.05)
        self.assertLess(
            order.index(('end', 'group:1', 'first')),
            order.index(('start', 'group:1', 'second')),
        )

    async def test_mailbox_event_precedes_pending_task(self):
        consumed = []
        done = asyncio.Event()

        async def consume(_scope_key, item):
            consumed.append(item['id'])
            if len(consumed) == 2:
                done.set()

        self.dispatcher = ScopeActorDispatcher(
            mailbox=self.mailbox,
            sessions=self.sessions,
            consume=consume,
        )
        self.dispatcher.submit_task('group:1', {'id': 'task'})
        self.dispatcher.submit_event(event('group', '1', 'message'), {'id': 'message'})

        await asyncio.wait_for(done.wait(), 1)
        self.assertEqual(consumed, ['message', 'task'])

    async def test_events_arriving_mid_turn_merge_into_one_local_followup(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._message_epoch = 1
        runtime._event_mailbox = self.mailbox
        runtime._turn_batch_coordinator = AtomicTurnBatchCoordinator(self.mailbox)
        runtime._character_sessions = self.sessions
        runtime._background_task_semaphore = asyncio.Semaphore(1)
        runtime._is_epoch_stale = lambda epoch: epoch is not None and epoch != 1
        runtime._is_message_stale = lambda _message: False
        runtime._process_task = None

        first_started = asyncio.Event()
        release_first = asyncio.Event()
        finished = asyncio.Event()
        turns = []

        async def run_message_turn(item):
            turns.append(item)
            if len(turns) == 1:
                first_started.set()
                await release_first.wait()
                return runtime._merge_followup_after_turn(item, True)
            finished.set()
            return runtime._merge_followup_after_turn(item, True)

        runtime._run_message_turn = run_message_turn
        self.dispatcher = ScopeActorDispatcher(
            mailbox=self.mailbox,
            sessions=self.sessions,
            consume=runtime._consume_scope_item,
            is_stale=lambda item: runtime._is_epoch_stale(item.get('message_epoch')),
        )
        runtime._scope_dispatcher = self.dispatcher

        def message_item(message_id, text):
            message = ChatMessage(
                chat_type='group',
                chat_id=1,
                user_id=2,
                text=text,
                raw_message=text,
                sender={'nickname': 'tester'},
                message_id=message_id,
                timestamp=float(message_id),
            )
            return {
                'kind': 'message',
                'message': message,
                'cleaned': text,
                'agent_id': 'group:1',
                'message_epoch': 1,
                'scope_key': 'group:1',
                'trigger_messages': [{'text': text, 'raw_message': text}],
            }

        first = message_item(1, 'first')
        first['followup_history_seed'] = [{'role': 'assistant', 'content': 'done'}]
        self.dispatcher.submit_event(envelope_from_scope_turn_item(first), first)
        await asyncio.wait_for(first_started.wait(), 1)

        second = message_item(2, 'second')
        third = message_item(3, 'third')
        self.dispatcher.submit_event(envelope_from_scope_turn_item(second), second)
        self.dispatcher.submit_event(envelope_from_scope_turn_item(third), third)
        release_first.set()

        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[1]['batch_metadata']['event_count'], 2)
        self.assertEqual(turns[1]['mailbox_event_ids'], ['2', '3'])
        self.assertEqual(
            [entry['text'] for entry in turns[1]['trigger_messages']],
            ['second', 'third'],
        )
        self.assertEqual(self.mailbox.pending_count('group:1'), 0)

    async def test_actor_keeps_consuming_followups_until_mailbox_clears(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._message_epoch = 1
        runtime._event_mailbox = self.mailbox
        runtime._turn_batch_coordinator = AtomicTurnBatchCoordinator(self.mailbox)
        runtime._character_sessions = self.sessions
        runtime._background_task_semaphore = asyncio.Semaphore(1)
        runtime._is_epoch_stale = lambda epoch: epoch is not None and epoch != 1
        runtime._is_message_stale = lambda _message: False
        runtime._process_task = None

        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()
        finished = asyncio.Event()
        turns = []

        async def run_message_turn(item):
            turns.append(item)
            item['followup_history_seed'] = [{'role': 'assistant', 'content': f"turn-{len(turns)}"}]
            item['turn_commit_evidence'] = {
                'outbound_history_committed': True,
                'turn_log_committed': True,
                'turn_metadata_committed': True,
            }
            item['completed_turn_metadata'] = {'turn_index': len(turns)}
            if len(turns) == 1:
                first_started.set()
                await release_first.wait()
            elif len(turns) == 2:
                second_started.set()
                await release_second.wait()
            else:
                finished.set()
            return runtime._merge_followup_after_turn(item, True)

        runtime._run_message_turn = run_message_turn
        self.dispatcher = ScopeActorDispatcher(
            mailbox=self.mailbox,
            sessions=self.sessions,
            consume=runtime._consume_scope_item,
            is_stale=lambda item: runtime._is_epoch_stale(item.get('message_epoch')),
        )
        runtime._scope_dispatcher = self.dispatcher

        def message_item(message_id, text):
            message = ChatMessage(
                chat_type='group',
                chat_id=1,
                user_id=2,
                text=text,
                raw_message=text,
                sender={'nickname': 'tester'},
                message_id=message_id,
                timestamp=float(message_id),
            )
            return {
                'kind': 'message',
                'message': message,
                'cleaned': text,
                'agent_id': 'group:1',
                'message_epoch': 1,
                'scope_key': 'group:1',
                'trigger_messages': [{'text': text, 'raw_message': text}],
            }

        self.dispatcher.submit_event(envelope_from_scope_turn_item(message_item(1, 'first')), message_item(1, 'first'))
        await asyncio.wait_for(first_started.wait(), 1)
        self.dispatcher.submit_event(envelope_from_scope_turn_item(message_item(2, 'second')), message_item(2, 'second'))
        self.dispatcher.submit_event(envelope_from_scope_turn_item(message_item(3, 'third')), message_item(3, 'third'))
        release_first.set()

        await asyncio.wait_for(second_started.wait(), 1)
        self.dispatcher.submit_event(envelope_from_scope_turn_item(message_item(4, 'fourth')), message_item(4, 'fourth'))
        release_second.set()

        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        self.assertEqual(len(turns), 3)
        self.assertEqual(
            [entry['text'] for entry in turns[1]['trigger_messages']],
            ['second', 'third'],
        )
        self.assertEqual(
            [entry['text'] for entry in turns[2]['trigger_messages']],
            ['fourth'],
        )
        self.assertEqual(self.mailbox.pending_count('group:1'), 0)

    async def test_new_dispatcher_starts_with_empty_runtime_state(self):
        consumed = []

        async def consume(_scope_key, item):
            consumed.append(item)

        self.dispatcher = ScopeActorDispatcher(
            mailbox=self.mailbox,
            sessions=self.sessions,
            consume=consume,
        )
        self.assertEqual(self.mailbox.pending_count(), 0)
        self.assertEqual(self.dispatcher.active_actor_count(), 0)
        self.assertEqual(consumed, [])


if __name__ == '__main__':
    unittest.main()
