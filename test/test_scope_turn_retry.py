import asyncio
import unittest

from core.ai_runtime import AIOrchestrator
from core.event_adapters import envelope_from_scope_turn_item
from core.event_mailbox import InMemoryEventMailbox
from core.event_batch_coordinator import AtomicTurnBatchCoordinator
from core.character_session import CharacterSessionRegistry
from core.scope_actor_dispatcher import ScopeActorDispatcher
from core.transport import ChatMessage


class ScopeTurnRetryTests(unittest.TestCase):
    """验证 scope turn 遇到上游不稳定时自动重试，永不放弃。"""

    def _make_runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._message_epoch = 1
        runtime._event_mailbox = InMemoryEventMailbox()
        runtime._turn_batch_coordinator = AtomicTurnBatchCoordinator(runtime._event_mailbox)
        runtime._character_sessions = CharacterSessionRegistry(mailbox=runtime._event_mailbox)
        runtime._background_task_semaphore = asyncio.Semaphore(1)
        runtime._scope_turn_retry_base_delay = 0.05
        runtime._scope_turn_retry_long_term_min = 0.15
        runtime._scope_turn_retry_max_delay = 0.3
        runtime.loop = None
        runtime._is_epoch_stale = lambda epoch: False
        runtime._is_message_stale = lambda message: False
        return runtime

    def test_soft_error_triggers_exponential_backoff_retry(self):
        runtime = self._make_runtime()
        call_log = []

        async def _process_failing(item):
            attempt = item['_mailbox_entry'].attempt
            call_log.append(attempt)
            if attempt < 2:
                raise RuntimeError('upstream timeout')

        runtime._process_task = _process_failing
        runtime._run_message_turn = _process_failing

        dispatcher = ScopeActorDispatcher(
            mailbox=runtime._event_mailbox,
            sessions=runtime._character_sessions,
            consume=runtime._consume_scope_item,
            is_stale=lambda item: runtime._is_epoch_stale(item.get('message_epoch')),
        )
        runtime._scope_dispatcher = dispatcher

        scope_key = 'private:123'
        message = ChatMessage(
            chat_type='private',
            chat_id=123,
            user_id=123,
            text='test',
            raw_message='test',
            sender={'user_id': 123},
            message_id=1,
        )
        message_item = {
            'kind': 'message',
            'scope_key': scope_key,
            'message': message,
            'cleaned': 'test',
            'message_epoch': 1,
        }

        async def run_test():
            envelope = envelope_from_scope_turn_item(message_item)
            dispatcher.submit_event(envelope, message_item)
            await asyncio.sleep(0.05)
            self.assertEqual(call_log, [0])
            self.assertEqual(runtime._event_mailbox.pending_count(scope_key), 1)

            await asyncio.sleep(0.08)
            dispatcher.wake(scope_key)
            await asyncio.sleep(0.05)
            self.assertEqual(call_log, [0, 1])
            self.assertEqual(runtime._event_mailbox.pending_count(scope_key), 1)

            await asyncio.sleep(0.15)
            dispatcher.wake(scope_key)
            await asyncio.sleep(0.05)
            self.assertEqual(call_log, [0, 1, 2])
            self.assertEqual(runtime._event_mailbox.pending_count(scope_key), 0)

            await dispatcher.close()

        asyncio.run(run_test())

    def test_hard_error_gives_up_immediately(self):
        runtime = self._make_runtime()
        call_log = []

        async def _process_hard_fail(item):
            call_log.append(item['_mailbox_entry'].attempt)
            raise ValueError('authentication failed')

        runtime._process_task = _process_hard_fail
        runtime._run_message_turn = _process_hard_fail

        dispatcher = ScopeActorDispatcher(
            mailbox=runtime._event_mailbox,
            sessions=runtime._character_sessions,
            consume=runtime._consume_scope_item,
            is_stale=lambda item: runtime._is_epoch_stale(item.get('message_epoch')),
        )
        runtime._scope_dispatcher = dispatcher

        scope_key = 'private:456'
        message = ChatMessage(
            chat_type='private',
            chat_id=456,
            user_id=456,
            text='test',
            raw_message='test',
            sender={'user_id': 456},
            message_id=2,
        )
        message_item = {
            'kind': 'message',
            'scope_key': scope_key,
            'message': message,
            'cleaned': 'test',
            'message_epoch': 1,
        }

        async def run_test():
            envelope = envelope_from_scope_turn_item(message_item)
            dispatcher.submit_event(envelope, message_item)
            await asyncio.sleep(0.05)
            self.assertEqual(call_log, [0])
            self.assertEqual(runtime._event_mailbox.pending_count(scope_key), 0)

            await dispatcher.close()

        asyncio.run(run_test())

    def test_never_gives_up_on_soft_errors(self):
        """软错误永不放弃，持续重试。"""
        runtime = self._make_runtime()
        call_log = []

        async def _process_always_fail(item):
            entry = item['_mailbox_entry']
            call_log.append(entry.attempt)
            raise RuntimeError('status=503 overloaded')

        runtime._process_task = _process_always_fail
        runtime._run_message_turn = _process_always_fail

        dispatcher = ScopeActorDispatcher(
            mailbox=runtime._event_mailbox,
            sessions=runtime._character_sessions,
            consume=runtime._consume_scope_item,
            is_stale=lambda item: runtime._is_epoch_stale(item.get('message_epoch')),
        )
        runtime._scope_dispatcher = dispatcher

        scope_key = 'private:789'
        message = ChatMessage(
            chat_type='private',
            chat_id=789,
            user_id=789,
            text='test',
            raw_message='test',
            sender={'user_id': 789},
            message_id=3,
        )
        message_item = {
            'kind': 'message',
            'scope_key': scope_key,
            'message': message,
            'cleaned': 'test',
            'message_epoch': 1,
        }

        async def run_test():
            envelope = envelope_from_scope_turn_item(message_item)
            dispatcher.submit_event(envelope, message_item)

            for i in range(25):
                await asyncio.sleep(0.05)
                dispatcher.wake(scope_key)

            await asyncio.sleep(0.1)
            self.assertGreaterEqual(len(call_log), 10)
            self.assertEqual(runtime._event_mailbox.pending_count(scope_key), 1)

            await dispatcher.close()

        asyncio.run(run_test())

    def test_delay_increases_over_attempts(self):
        """验证重试延迟随 attempt 增加：快速 → 长期 → 最大。"""
        runtime = self._make_runtime()
        delays = [runtime._compute_retry_delay(i) for i in range(12)]

        # 快速重试阶段：0-1
        self.assertLess(delays[0], delays[1])
        # 到达 long_term_min 上限
        self.assertEqual(delays[2], runtime._scope_turn_retry_long_term_min)
        # 长期重试阶段：6-8 倍增
        self.assertLess(delays[6], delays[7])
        # 到达 max_delay
        self.assertEqual(delays[7], runtime._scope_turn_retry_max_delay)
        # 稳态：9+
        self.assertEqual(delays[9], delays[11])
        self.assertEqual(delays[9], runtime._scope_turn_retry_max_delay)

    def test_mailbox_entry_preserves_attempt_across_requeue(self):
        """验证 requeue_front 正确递增 attempt 计数。"""
        mailbox = InMemoryEventMailbox()
        from core.event_envelope import EventEnvelope, EventType

        envelope = EventEnvelope(
            event_type=EventType.MESSAGE,
            scope_type='test',
            scope_id='1',
            source='test',
        )
        entry1 = mailbox.append_entry(envelope, transient={'data': 1})
        self.assertEqual(entry1.attempt, 0)

        popped = mailbox.pop_scope_entry('test:1')
        self.assertEqual(popped.attempt, 0)

        entry2 = mailbox.requeue_front(popped.envelope, popped.transient, attempt=1, delay=0.0)
        self.assertEqual(entry2.attempt, 1)

        popped2 = mailbox.pop_scope_entry('test:1')
        self.assertEqual(popped2.attempt, 1)


if __name__ == '__main__':
    unittest.main()
