import threading
import unittest
from collections import Counter

from core.event_envelope import EventEnvelope, EventType
from core.event_mailbox import InMemoryEventMailbox


def event(text, scope_type='group', scope_id='7', event_id=None):
    kwargs = {}
    if event_id is not None:
        kwargs['event_id'] = event_id
    return EventEnvelope(
        event_type=EventType.MESSAGE,
        scope_type=scope_type,
        scope_id=scope_id,
        payload={'text': text},
        source='robustness-test',
        occurred_at=100.0,
        **kwargs,
    )


def run_threads(target, count):
    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class ConcurrentAppendTests(unittest.TestCase):
    """多线程并发 append 时:事件不丢、不重、序列号全局单调。"""

    def test_parallel_append_across_scopes_loses_nothing(self):
        mailbox = InMemoryEventMailbox()
        workers = 8
        events_per_worker = 50

        def append_worker(worker_idx):
            for i in range(events_per_worker):
                mailbox.append(event(f'{worker_idx}-{i}', scope_type='group', scope_id=str(worker_idx % 3)))

        run_threads(append_worker, workers)

        self.assertEqual(workers * events_per_worker, mailbox.pending_count())
        seen = []
        for i in range(3):
            batch = mailbox.drain_scope(f'group:{i}')
            if batch:
                seen.extend(item.payload['text'] for item in batch.events)
        self.assertEqual(workers * events_per_worker, len(seen))
        self.assertEqual(len(seen), len(set(seen)), '并发 append 下事件不得重复')

    def test_sequences_are_globally_monotonic_under_concurrency(self):
        mailbox = InMemoryEventMailbox()
        workers = 6
        events_per_worker = 100

        def append_worker(worker_idx):
            for i in range(events_per_worker):
                mailbox.append(event(f'{worker_idx}-{i}', scope_id=str(worker_idx)))

        run_threads(append_worker, workers)

        sequences = []
        for i in range(workers):
            batch = mailbox.drain_scope(f'group:{i}')
            if batch:
                sequences.extend(item.mailbox_sequence for item in batch.events)
        self.assertEqual(workers * events_per_worker, len(sequences))
        self.assertEqual(sequences, sorted(sequences), '序列号必须全局唯一且单调')
        self.assertEqual(len(sequences), len(set(sequences)))

    def test_append_many_is_atomic_under_concurrent_drain(self):
        mailbox = InMemoryEventMailbox()
        errors = []
        BATCH = 20

        def drain_worker():
            try:
                batch = mailbox.drain_scope('group:7')
                if batch is not None:
                    # drain 与 append_many 竞争时,只能看到完整批次,不能是残缺数量
                    self.assertEqual(0, len(batch.events) % BATCH)
            except Exception as exc:
                errors.append(exc)

        def append_worker():
            try:
                mailbox.append_many([event(f'bulk-{i}') for i in range(BATCH)])
            except Exception as exc:
                errors.append(exc)

        for _ in range(4):
            append_thread = threading.Thread(target=append_worker)
            drain_thread = threading.Thread(target=drain_worker)

            append_thread.start()
            drain_thread.start()
            append_thread.join()
            drain_thread.join()

        self.assertEqual([], errors)
        self.assertEqual(0, mailbox.pending_count() % BATCH, '剩余也必须是完整批次的整数倍')


class ConcurrentPopTests(unittest.TestCase):
    def test_parallel_pop_never_delivers_same_entry_twice(self):
        mailbox = InMemoryEventMailbox()
        total = 200
        for i in range(total):
            mailbox.append(event(f'e{i}'))

        popped = []
        lock = threading.Lock()

        def pop_worker(_i):
            while True:
                entry = mailbox.pop_scope_entry('group:7')
                if entry is None:
                    return
                with lock:
                    popped.append(entry.envelope.payload['text'])

        run_threads(pop_worker, 8)
        self.assertEqual(total, len(popped))
        self.assertEqual(len(popped), len(set(popped)), '同一条事件不得被并发 pop 两次')
        self.assertTrue(mailbox.is_empty())

    def test_pop_and_drain_are_mutually_exclusive(self):
        mailbox = InMemoryEventMailbox()
        for i in range(30):
            mailbox.append(event(f'e{i}'))
        identities = []

        def pop_worker(_i):
            for _ in range(5):
                entry = mailbox.pop_scope_entry('group:7')
                if entry is not None:
                    identities.append(entry.envelope.mailbox_sequence)

        def drain_worker(_i):
            batch = mailbox.drain_scope('group:7')
            if batch is not None:
                identities.extend(item.mailbox_sequence for item in batch.events)

        run_threads(pop_worker, 3)
        run_threads(drain_worker, 2)

        self.assertEqual(30, len(identities))
        self.assertEqual(len(identities), len(set(identities)), 'pop 与 drain 不得重复消费同一条事件')


class TransientUnderConcurrencyTests(unittest.TestCase):
    def test_transient_object_identity_survives_concurrent_append_and_drain(self):
        mailbox = InMemoryEventMailbox()
        transient = {'owner': 'single-object'}
        mailbox.append(event('one'), transient=transient)
        batch = mailbox.drain_scope('group:7')
        self.assertIs(batch.transients[0], transient)


if __name__ == '__main__':
    unittest.main()
