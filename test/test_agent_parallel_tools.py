import asyncio
import threading
import time
import unittest
from dataclasses import dataclass

from core.dev_agent import (
    PARALLEL_GITHUB_READ_TOOLS,
    PARALLEL_LOCAL_READ_TOOLS,
    _budget_tool_results,
    _call_with_retry,
    _execute_tool_calls_ordered,
)


@dataclass
class FakeCall:
    call_id: str
    name: str
    input: dict


class ParallelToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_reads_preserve_original_order(self):
        delays = {'a': 0.08, 'b': 0.01, 'c': 0.04}
        active = 0
        max_active = 0
        lock = threading.Lock()

        def execute(name, tool_input, *_args):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(delays[tool_input['key']])
                return tool_input['key'].upper()
            finally:
                with lock:
                    active -= 1

        calls = [
            FakeCall('id-a', 'read_local_file', {'key': 'a'}),
            FakeCall('id-b', 'search_local_file', {'key': 'b'}),
            FakeCall('id-c', 'list_local_files', {'key': 'c'}),
        ]
        results = await _execute_tool_calls_ordered(
            calls, '.', '', None, execute_fn=execute, total_result_limit=10_000,
        )
        self.assertEqual(results, ['A', 'B', 'C'])
        self.assertGreaterEqual(max_active, 2)

    async def test_write_and_shell_calls_are_strict_barriers(self):
        events = []
        lock = threading.Lock()

        def execute(name, tool_input, *_args):
            key = tool_input['key']
            with lock:
                events.append(('start', key))
            if name in PARALLEL_LOCAL_READ_TOOLS:
                time.sleep(0.04)
            else:
                time.sleep(0.01)
            with lock:
                events.append(('end', key))
            return key

        calls = [
            FakeCall('r1', 'read_local_file', {'key': 'read-before-1'}),
            FakeCall('r2', 'search_local_file', {'key': 'read-before-2'}),
            FakeCall('w', 'replace_local_file_text', {'key': 'write'}),
            FakeCall('r3', 'read_local_file', {'key': 'read-after'}),
            FakeCall('s', 'shell_exec', {'key': 'shell'}),
        ]
        results = await _execute_tool_calls_ordered(
            calls, '.', '', None, execute_fn=execute, total_result_limit=10_000,
        )
        self.assertEqual(results, ['read-before-1', 'read-before-2', 'write', 'read-after', 'shell'])
        positions = {event: index for index, event in enumerate(events)}
        self.assertLess(positions[('end', 'read-before-1')], positions[('start', 'write')])
        self.assertLess(positions[('end', 'read-before-2')], positions[('start', 'write')])
        self.assertLess(positions[('end', 'write')], positions[('start', 'read-after')])
        self.assertLess(positions[('end', 'read-after')], positions[('start', 'shell')])

    async def test_single_failure_does_not_cancel_parallel_batch(self):
        executed = []
        lock = threading.Lock()

        def execute(_name, tool_input, *_args):
            with lock:
                executed.append(tool_input['key'])
            if tool_input['key'] == 'bad':
                raise RuntimeError('boom')
            time.sleep(0.02)
            return f"ok-{tool_input['key']}"

        calls = [
            FakeCall('a', 'read_local_file', {'key': 'a'}),
            FakeCall('bad', 'search_local_file', {'key': 'bad'}),
            FakeCall('c', 'read_local_file_chunk', {'key': 'c'}),
        ]
        results = await _execute_tool_calls_ordered(
            calls, '.', '', None, execute_fn=execute, total_result_limit=10_000,
        )
        self.assertEqual(results[0], 'ok-a')
        self.assertIn('RuntimeError: boom', results[1])
        self.assertEqual(results[2], 'ok-c')
        self.assertCountEqual(executed, ['a', 'bad', 'c'])

    async def test_local_and_github_concurrency_are_limited_separately(self):
        counters = {'local': 0, 'github': 0, 'local_max': 0, 'github_max': 0}
        lock = threading.Lock()

        def execute(name, _tool_input, *_args):
            group = 'github' if name in PARALLEL_GITHUB_READ_TOOLS else 'local'
            with lock:
                counters[group] += 1
                counters[f'{group}_max'] = max(counters[f'{group}_max'], counters[group])
            time.sleep(0.04)
            with lock:
                counters[group] -= 1
            return name

        calls = [
            *[FakeCall(f'l{i}', 'read_local_file', {'i': i}) for i in range(6)],
            *[FakeCall(f'g{i}', 'github_read_file', {'i': i}) for i in range(5)],
        ]
        await _execute_tool_calls_ordered(
            calls, '.', '', None, execute_fn=execute,
            local_concurrency=3, github_concurrency=2, max_parallel_sub_batch=20,
            total_result_limit=20_000,
        )
        self.assertLessEqual(counters['local_max'], 3)
        self.assertLessEqual(counters['github_max'], 2)
        self.assertGreaterEqual(counters['local_max'], 2)
        self.assertGreaterEqual(counters['github_max'], 2)

    async def test_unknown_tool_defaults_to_serial_barrier(self):
        events = []

        def execute(name, tool_input, *_args):
            events.append(('start', tool_input['key']))
            time.sleep(0.01)
            events.append(('end', tool_input['key']))
            return name

        calls = [
            FakeCall('r', 'read_local_file', {'key': 'read'}),
            FakeCall('u', 'future_unknown_tool', {'key': 'unknown'}),
            FakeCall('r2', 'read_local_file', {'key': 'read2'}),
        ]
        await _execute_tool_calls_ordered(calls, '.', '', None, execute_fn=execute)
        self.assertLess(events.index(('end', 'read')), events.index(('start', 'unknown')))
        self.assertLess(events.index(('end', 'unknown')), events.index(('start', 'read2')))

    async def test_github_write_is_a_serial_barrier(self):
        events = []

        def execute(_name, tool_input, *_args):
            key = tool_input['key']
            events.append(('start', key))
            time.sleep(0.02)
            events.append(('end', key))
            return key

        calls = [
            FakeCall('g-read', 'github_read_file', {'key': 'github-read'}),
            FakeCall('g-write', 'github_create_or_update_file', {'key': 'github-write'}),
            FakeCall('local-read', 'read_local_file', {'key': 'local-read'}),
        ]
        results = await _execute_tool_calls_ordered(calls, '.', '', None, execute_fn=execute)
        self.assertEqual(results, ['github-read', 'github-write', 'local-read'])
        self.assertLess(events.index(('end', 'github-read')), events.index(('start', 'github-write')))
        self.assertLess(events.index(('end', 'github-write')), events.index(('start', 'local-read')))

    def test_result_budget_keeps_small_results_and_makes_large_results_followable(self):
        calls = [
            FakeCall('small', 'search_local_file', {'path': 'a.py', 'query': 'x'}),
            FakeCall('large', 'read_local_file_chunk', {
                'path': 'large.py', 'start_line': 1, 'line_count': 120,
            }),
        ]
        large = 'HEAD\n' + ('x' * 20_000) + '\nTAIL'
        results = _budget_tool_results(calls, ['small-result', large], total_limit=5_000)
        self.assertEqual(results[0], 'small-result')
        self.assertIn('结果过长已截断', results[1])
        self.assertIn('start_line=121', results[1])
        self.assertTrue(results[1].endswith('TAIL'))
        self.assertLessEqual(sum(len(item) for item in results), 5_300)

    def test_total_budget_is_a_hard_limit_even_with_many_calls(self):
        calls = [
            FakeCall(str(i), 'read_local_file', {'path': f'{i}.py'})
            for i in range(12)
        ]
        results = _budget_tool_results(calls, ['x' * 5_000] * len(calls), total_limit=1_000)
        self.assertEqual(len(results), len(calls))
        self.assertLessEqual(sum(len(item) for item in results), 1_000)

    def test_edit_result_keeps_diff_header_and_tail_diagnostic(self):
        call = FakeCall('edit', 'apply_unified_diff_to_file', {'path': 'target.py'})
        diff = '--- a/target.py\n+++ b/target.py\n' + ('middle\n' * 15_000) + 'FINAL VALIDATION ERROR\n'
        result = _budget_tool_results([call], [diff], total_limit=8_000)[0]
        self.assertTrue(result.startswith('--- a/target.py'))
        self.assertIn('结果过长已截断', result)
        self.assertIn('git diff -- <path>', result)
        self.assertTrue(result.endswith('FINAL VALIDATION ERROR\n'))
        self.assertLessEqual(len(result), 8_000)

    async def test_parallel_sub_batch_limit_caps_total_active_calls(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def execute(_name, _tool_input, *_args):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return 'ok'

        calls = [FakeCall(str(i), 'read_local_file', {'i': i}) for i in range(7)]
        await _execute_tool_calls_ordered(
            calls, '.', '', None, execute_fn=execute,
            local_concurrency=10, max_parallel_sub_batch=2,
        )
        self.assertLessEqual(max_active, 2)
        self.assertGreaterEqual(max_active, 2)


class RetryCompatibilityTests(unittest.TestCase):
    def test_retryable_error_is_retried_and_eventually_succeeds(self):
        attempts = {'count': 0}

        def operation():
            attempts['count'] += 1
            if attempts['count'] < 3:
                raise RuntimeError('503 temporarily unavailable')
            return 'ok'

        from unittest.mock import patch
        with patch('core.dev_agent._retry_sleep_seconds', return_value=0), patch('core.dev_agent.time.sleep'):
            result = _call_with_retry('test', operation, max_retries=3)
        self.assertEqual(result, 'ok')
        self.assertEqual(attempts['count'], 3)

    def test_non_retryable_error_is_not_retried(self):
        attempts = {'count': 0}

        def operation():
            attempts['count'] += 1
            raise ValueError('invalid input')

        with self.assertRaises(ValueError):
            _call_with_retry('test', operation, max_retries=3)
        self.assertEqual(attempts['count'], 1)


class AgentPromptRulesTests(unittest.TestCase):
    def test_prompt_preserves_state_safety_and_parallel_rules(self):
        with open('data/prompt/agent.txt', 'r', encoding='utf-8') as handle:
            prompt = handle.read()
        required_phrases = [
            '[[AGENT_DONE]]',
            '纯文本',
            '号主',
            '提示词注入',
            '持久化',
            '重试',
            '总结',
            '同一次响应中批量发出',
            '禁止伪并行依赖链',
            '文件写入/编辑、shell、GitHub 写操作',
            '无目的扫描仓库',
        ]
        for phrase in required_phrases:
            self.assertIn(phrase, prompt)


if __name__ == '__main__':
    unittest.main()
