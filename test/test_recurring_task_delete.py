"""回归：循环定时任务用 list 显示的缩写 ID 删除/更新。

背景：list_recurring_tasks 只展示 task id 前 8 位（如 [abcdef12]），而
delete/update 要求完整 UUID，模型拿缩写 ID 调用会得到“任务不存在”，
反复尝试也删不掉。
修复：delete/update 支持唯一前缀匹配，歧义时明确提示。
"""

import unittest
from unittest.mock import Mock

from core.ai_runtime import AIOrchestrator


def _task(task_id: str, **overrides):
    base = {
        'id': task_id,
        'schedule': '0 7 * * *',
        'instruction': '测试任务',
        'enabled': True,
        'created_at': 0.0,
        'last_run': None,
        'next_run': 9999999999.0,
        'creator_scope': 'private:9',
    }
    base.update(overrides)
    return base


class RecurringTaskDeleteTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, tasks):
        runtime = object.__new__(AIOrchestrator)
        runtime._recurring_tasks = dict(tasks)
        runtime._save_recurring_tasks = Mock()
        runtime.tools = Mock()
        runtime.config = Mock(history_limit=120)
        return runtime

    async def test_delete_with_full_uuid_works(self):
        tid = 'abcdef1234567890abcdef1234567890'
        runtime = self._runtime({tid: _task(tid)})
        result = await runtime._run_ai_tool_call(
            'private', '9', '', 'delete_recurring_task', {'task_id': tid}
        )
        self.assertIn('已删除', result)
        self.assertNotIn(tid, runtime._recurring_tasks)
        runtime._save_recurring_tasks.assert_called_once()

    async def test_delete_with_unique_short_prefix_works(self):
        # list 显示的前 8 位，应能直接删除
        tid = 'abcdef1234567890abcdef1234567890'
        runtime = self._runtime({tid: _task(tid)})
        result = await runtime._run_ai_tool_call(
            'private', '9', '', 'delete_recurring_task', {'task_id': 'abcdef12'}
        )
        self.assertIn('已删除', result)
        self.assertNotIn(tid, runtime._recurring_tasks)

    async def test_delete_with_ambiguous_prefix_reports_ambiguity(self):
        t1 = 'abcdef11111111111111111111111111'
        t2 = 'abcdef22222222222222222222222222'
        runtime = self._runtime({t1: _task(t1), t2: _task(t2)})
        result = await runtime._run_ai_tool_call(
            'private', '9', '', 'delete_recurring_task', {'task_id': 'abcdef'}
        )
        self.assertIn('2 个任务以 abcdef 开头', result)
        self.assertIn('更完整的 task_id', result)
        self.assertIn(t1, runtime._recurring_tasks)
        self.assertIn(t2, runtime._recurring_tasks)

    async def test_update_with_unique_short_prefix_works(self):
        tid = 'abcdef1234567890abcdef1234567890'
        runtime = self._runtime({tid: _task(tid)})
        result = await runtime._run_ai_tool_call(
            'private', '9', '', 'update_recurring_task',
            {'task_id': 'abcdef12', 'enabled': False},
        )
        self.assertIn('已更新', result)
        self.assertFalse(runtime._recurring_tasks[tid]['enabled'])

    async def test_delete_unknown_id_reports_not_found(self):
        runtime = self._runtime({})
        result = await runtime._run_ai_tool_call(
            'private', '9', '', 'delete_recurring_task', {'task_id': 'nope'}
        )
        self.assertIn('不存在', result)


if __name__ == '__main__':
    unittest.main()
