import tempfile
import threading
import unittest
from pathlib import Path

from core.token_usage_store import TokenUsageStore


class TokenUsageStoreTests(unittest.TestCase):
    def make_store(self, directory):
        return TokenUsageStore(str(Path(directory) / 'token_usage.json'))

    def test_accumulates_global_and_isolates_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            store.record(10, 2, scope_key='private:1')
            store.record(20, 3, estimated=True, scope_key='group:2')
            store.record(5, 1, scope_key=None)

            first = store.snapshot('private:1')
            second = store.snapshot('group:2')
            self.assertEqual(first['scope']['input_tokens'], 10)
            self.assertEqual(first['scope']['output_tokens'], 2)
            self.assertEqual(second['scope']['input_tokens'], 20)
            self.assertEqual(second['scope']['estimated_call_count'], 1)
            self.assertEqual(first['global']['input_tokens'], 35)
            self.assertEqual(first['global']['output_tokens'], 6)
            self.assertEqual(first['global']['call_count'], 3)
            self.assertEqual(first['global']['estimated_call_count'], 1)

    def test_restart_restores_counters_and_last_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            store.record(11, 4, estimated=True, model='m', scope_key='private:1')
            restored = self.make_store(tmp).snapshot('private:1')
            self.assertEqual(restored['global']['input_tokens'], 11)
            self.assertEqual(restored['scope']['output_tokens'], 4)
            self.assertTrue(restored['last']['estimated'])

    def test_concurrent_updates_do_not_lose_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            threads = [
                threading.Thread(
                    target=lambda: [store.record(1, 2, scope_key='private:1') for _ in range(25)]
                )
                for _ in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            snapshot = store.snapshot('private:1')
            self.assertEqual(snapshot['global']['call_count'], 200)
            self.assertEqual(snapshot['global']['input_tokens'], 200)
            self.assertEqual(snapshot['scope']['output_tokens'], 400)

    def test_missing_usage_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            self.assertIsNone(store.record(None, 2, scope_key='private:1'))
            self.assertEqual(store.snapshot('private:1')['global']['call_count'], 0)


if __name__ == '__main__':
    unittest.main()
