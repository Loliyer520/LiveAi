import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pack.json_store import JsonStore


class _Base(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / 'state.json'
        self.addCleanup(self._dir.cleanup)

    def _store(self):
        return JsonStore(str(self.path))

    def _on_disk(self):
        return json.loads(self.path.read_text(encoding='utf-8'))


class TempNameTests(_Base):
    """固定的 .tmp 名在有第二个写入者时会互相覆盖，也更容易撞文件占用。"""

    def test_temp_name_is_unique_per_call(self):
        store = self._store()
        seen = []

        real_replace = Path.replace

        def _spy(self_path, target):
            seen.append(self_path.name)
            return real_replace(self_path, target)

        with patch.object(Path, 'replace', _spy):
            store.save({'a': 1})
            store.save({'a': 2})

        self.assertEqual(2, len(seen))
        self.assertNotEqual(seen[0], seen[1], '临时名必须每次不同')

    def test_temp_name_carries_pid_and_thread(self):
        store = self._store()
        seen = []
        real_replace = Path.replace

        with patch.object(Path, 'replace', lambda p, t: (seen.append(p.name), real_replace(p, t))[1]):
            store.save({'a': 1})

        self.assertIn(str(os.getpid()), seen[0])
        self.assertIn(str(threading.get_ident()), seen[0])
        self.assertTrue(seen[0].endswith('.tmp'))

    def test_no_temp_file_left_behind_on_success(self):
        self._store().save({'a': 1})
        self.assertEqual(['state.json'], [p.name for p in Path(self._dir.name).iterdir()])


class ReplaceRetryTests(_Base):
    """WinError 32：目标被杀软/索引器/另一进程短暂持有句柄。"""

    def test_retries_then_succeeds(self):
        store = self._store()
        calls = {'n': 0}
        real_replace = Path.replace

        def _flaky(self_path, target):
            calls['n'] += 1
            if calls['n'] < 3:
                raise OSError(32, 'being used by another process')
            return real_replace(self_path, target)

        with patch.object(Path, 'replace', _flaky), patch('pack.json_store.time.sleep'):
            store.save({'ok': True})

        self.assertEqual(3, calls['n'])
        self.assertEqual({'ok': True}, self._on_disk())

    def test_raises_after_attempts_exhausted(self):
        store = self._store()

        def _always(self_path, target):
            raise OSError(32, 'being used by another process')

        with patch.object(Path, 'replace', _always), patch('pack.json_store.time.sleep'):
            with self.assertRaises(OSError):
                store.save({'ok': True})

    def test_backoff_grows(self):
        store = self._store()
        slept = []

        def _always(self_path, target):
            raise OSError(32, 'locked')

        with patch.object(Path, 'replace', _always), patch('pack.json_store.time.sleep', slept.append):
            with self.assertRaises(OSError):
                store.save({'a': 1})

        self.assertEqual(JsonStore._REPLACE_ATTEMPTS - 1, len(slept), '最后一次失败不再 sleep')
        self.assertEqual(sorted(slept), slept)

    def test_temp_file_cleaned_up_on_failure(self):
        store = self._store()

        def _always(self_path, target):
            raise OSError(32, 'locked')

        with patch.object(Path, 'replace', _always), patch('pack.json_store.time.sleep'):
            with self.assertRaises(OSError):
                store.save({'a': 1})

        leftovers = [p.name for p in Path(self._dir.name).iterdir() if p.name.endswith('.tmp')]
        self.assertEqual([], leftovers, '写失败也不该留下临时文件')


class CacheConsistencyTests(_Base):
    """写失败时缓存必须留在旧值，否则调用方以为存下来了，重启即丢。"""

    def test_cache_not_updated_when_replace_fails(self):
        store = self._store()
        store.save({'v': 1})

        def _always(self_path, target):
            raise OSError(32, 'locked')

        with patch.object(Path, 'replace', _always), patch('pack.json_store.time.sleep'):
            with self.assertRaises(OSError):
                store.save({'v': 2})

        self.assertEqual({'v': 1}, store.load())
        self.assertEqual({'v': 1}, self._on_disk())

    def test_cache_updated_on_success(self):
        store = self._store()
        store.save({'v': 1})
        store.save({'v': 2})
        self.assertEqual({'v': 2}, store.load())

    def test_failed_update_does_not_leak_mutation(self):
        store = self._store()
        store.save({'agents': {'a': 1}})

        def _always(self_path, target):
            raise OSError(32, 'locked')

        with patch.object(Path, 'replace', _always), patch('pack.json_store.time.sleep'):
            with self.assertRaises(OSError):
                store.update(lambda d: d['agents'].pop('a'))

        self.assertEqual({'agents': {'a': 1}}, store.load())

    def test_successful_update_persists(self):
        store = self._store()
        store.save({'agents': {'a': 1, 'b': 2}})
        store.update(lambda d: d['agents'].pop('a'))
        self.assertEqual({'agents': {'b': 2}}, self._on_disk())


class ConcurrencyTests(_Base):
    def test_parallel_saves_all_land(self):
        store = self._store()
        store.save({'n': 0})
        errors = []

        def _worker(i):
            try:
                store.update(lambda d: d.__setitem__(f'k{i}', i))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual([], errors)
        disk = self._on_disk()
        for i in range(12):
            self.assertEqual(i, disk[f'k{i}'])


if __name__ == '__main__':
    unittest.main()
