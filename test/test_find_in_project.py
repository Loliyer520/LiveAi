import os
import tempfile
import unittest

from core import dev_agent
from core.dev_agent import (
    _build_tools_schema,
    _find_in_project,
    _list_local_files,
    _read_local_file,
    _resolve_safe_path,
)


class FindInProjectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self._write('core/ai_runtime.py', 'class AIOrchestrator:\n    pass\n')
        self._write('core/dev_agent.py', 'MAX_ITERATIONS = 100\n')
        self._write('pack/napcat.py', 'def send_message():\n    return None\n')
        self._write('node_modules/pkg/ai_runtime.py', 'noise\n')
        self._write('.git/objects/ai_runtime.py', 'noise\n')
        self._write('data/state/ai_runtime.py', 'secret\n')

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, relative, text):
        path = os.path.join(self.root, relative.replace('/', os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def test_requires_at_least_one_criterion(self):
        self.assertIn('至少', _find_in_project(self.root))

    def test_finds_file_by_name_in_one_call(self):
        result = _find_in_project(self.root, name_pattern='ai_runtime.py')

        self.assertIn('core/ai_runtime.py', result)

    def test_substring_pattern_without_wildcards_matches(self):
        # 模型经常只写 runtime，不该因为没有 * 就一条都命中不到
        self.assertIn('core/ai_runtime.py', _find_in_project(self.root, name_pattern='runtime'))

    def test_wildcard_pattern_matches(self):
        result = _find_in_project(self.root, name_pattern='*_agent.py')

        self.assertIn('core/dev_agent.py', result)
        self.assertNotIn('core/ai_runtime.py', result)

    def test_skips_noise_directories(self):
        result = _find_in_project(self.root, name_pattern='ai_runtime.py')

        self.assertNotIn('node_modules', result)
        self.assertNotIn('.git', result)

    def test_skips_denylisted_paths(self):
        # data/state 属于 DENYLIST_PREFIXES，递归搜索不能把它捞出来
        self.assertNotIn('data/state', _find_in_project(self.root, name_pattern='ai_runtime.py'))

    def test_content_search_reports_path_and_line(self):
        result = _find_in_project(self.root, content_query='send_message')

        self.assertIn('pack/napcat.py:1', result)

    def test_content_regex_search(self):
        result = _find_in_project(self.root, content_query=r'MAX_\w+ = \d+', is_regex=True)

        self.assertIn('core/dev_agent.py:1', result)

    def test_invalid_regex_is_reported(self):
        self.assertIn('正则表达式无效', _find_in_project(self.root, content_query='(', is_regex=True))

    def test_name_and_content_combine(self):
        result = _find_in_project(self.root, name_pattern='*.py', content_query='AIOrchestrator')

        self.assertIn('core/ai_runtime.py:1', result)
        self.assertNotIn('napcat.py', result)

    def test_subpath_limits_scope(self):
        result = _find_in_project(self.root, name_pattern='*.py', subpath='pack')

        self.assertIn('pack/napcat.py', result)
        self.assertNotIn('core/ai_runtime.py', result)

    def test_subpath_outside_root_is_rejected(self):
        self.assertIn('拒绝访问', _find_in_project(self.root, name_pattern='*.py', subpath='../..'))

    def test_missing_subpath_is_reported(self):
        self.assertIn('不存在', _find_in_project(self.root, name_pattern='*.py', subpath='nope'))

    def test_no_match_is_reported(self):
        self.assertIn('未找到匹配项', _find_in_project(self.root, name_pattern='zzz_nothing.py'))

    def test_max_results_is_capped_and_flagged(self):
        result = _find_in_project(self.root, name_pattern='*.py', max_results=1)

        self.assertIn('max_results=1', result)
        self.assertEqual(len(result.splitlines()), 3)


class ReadOnlyProjectPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, 'core'), exist_ok=True)
        with open(os.path.join(self.root, 'core', 'sample.py'), 'w', encoding='utf-8') as handle:
            handle.write('print("ok")\n')
        os.makedirs(os.path.join(self.root, 'data', 'state'), exist_ok=True)
        with open(os.path.join(self.root, 'data', 'state', 'secret.txt'), 'w', encoding='utf-8') as handle:
            handle.write('secret')

    def tearDown(self):
        self._tmp.cleanup()

    def test_lists_directory_and_reads_text_file(self):
        self.assertIn('sample.py', _list_local_files(self.root, 'core'))
        self.assertEqual('print("ok")\n', _read_local_file(self.root, 'core/sample.py'))

    def test_rejects_parent_absolute_and_sensitive_paths(self):
        for path in ('../outside.txt', os.path.abspath(__file__), 'data/state/secret.txt'):
            self.assertIsNone(_resolve_safe_path(self.root, path))

    def test_rejects_symlink_escape(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = os.path.join(outside.name, 'secret.txt')
        with open(outside_file, 'w', encoding='utf-8') as handle:
            handle.write('secret')
        link = os.path.join(self.root, 'linked-secret.txt')
        try:
            os.symlink(outside_file, link)
        except (OSError, NotImplementedError):
            self.skipTest('当前环境不允许创建符号链接')
        self.assertIsNone(_resolve_safe_path(self.root, 'linked-secret.txt'))
        self.assertIn('拒绝读取', _read_local_file(self.root, 'linked-secret.txt'))


class FindInProjectRegistrationTests(unittest.TestCase):
    def test_tool_is_exposed_in_schema(self):
        for read_only in (False, True):
            names = [tool['name'] for tool in _build_tools_schema(read_only=read_only)]
            self.assertIn('find_in_project', names)

    def test_tool_is_parallel_and_read_only(self):
        self.assertIn('find_in_project', dev_agent.PARALLEL_READ_TOOLS)
        self.assertIn('find_in_project', dev_agent.READ_ONLY_AGENT_TOOLS)
        self.assertNotIn('find_in_project', dev_agent.LOCAL_WRITE_TOOLS)

    def test_tool_has_result_budget(self):
        self.assertIn('find_in_project', dev_agent.TOOL_RESULT_LIMITS)


if __name__ == '__main__':
    unittest.main()
