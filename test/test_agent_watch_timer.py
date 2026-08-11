import time
import unittest
from types import SimpleNamespace

from core.ai_runtime import AIOrchestrator


class _FakeManager:
    def __init__(self, agents=None, raise_on_list=False):
        self._agents = agents or []
        self._raise = raise_on_list

    def list_agents(self):
        if self._raise:
            raise RuntimeError('boom')
        return self._agents


def _agent(agent_id='a1b2c3d4e5', status='running', scope='group:7', idle=0.0, summary='查日志'):
    return {
        'agent_id': agent_id,
        'status': status,
        'origin_scope': scope,
        'instruction_summary': summary,
        'last_activity_at': time.time() - idle,
        'updated_at': time.time() - idle,
    }


class AgentWatchTimerTests(unittest.TestCase):
    def _runtime(self, agents=None, raise_on_list=False):
        runtime = object.__new__(AIOrchestrator)
        runtime._recurring_tasks = {}
        runtime._save_recurring_tasks = lambda: None
        runtime.agent_manager = _FakeManager(agents, raise_on_list)
        return runtime

    # ── 创建与复用 ────────────────────────────────────────────────
    def test_creates_timer_for_scope(self):
        runtime = self._runtime()
        note = runtime._ensure_agent_watch_timer('group', 7)

        self.assertEqual(len(runtime._recurring_tasks), 1)
        task = next(iter(runtime._recurring_tasks.values()))
        self.assertEqual(task['kind'], AIOrchestrator.AGENT_WATCH_TASK_KIND)
        self.assertEqual(task['target_scope'], 'group:7')
        self.assertTrue(task['enabled'])
        self.assertGreater(task['next_run'], time.time())
        self.assertIn('巡检定时器', note)

    def test_second_agent_in_same_scope_reuses_timer(self):
        runtime = self._runtime()
        runtime._ensure_agent_watch_timer('group', 7)
        note = runtime._ensure_agent_watch_timer('group', 7)

        # 同 scope 再派 agent 不能再开一个定时器，否则汇报会翻倍
        self.assertEqual(len(runtime._recurring_tasks), 1)
        self.assertIn('复用', note)

    def test_different_scopes_get_separate_timers(self):
        runtime = self._runtime()
        runtime._ensure_agent_watch_timer('group', 7)
        runtime._ensure_agent_watch_timer('private', 42)

        self.assertEqual(len(runtime._recurring_tasks), 2)
        scopes = {t['target_scope'] for t in runtime._recurring_tasks.values()}
        self.assertEqual(scopes, {'group:7', 'private:42'})

    def test_reuse_reenables_disabled_timer(self):
        runtime = self._runtime()
        runtime._ensure_agent_watch_timer('group', 7)
        task = next(iter(runtime._recurring_tasks.values()))
        task['enabled'] = False
        task['schedule_error'] = 'invalid target_scope'

        runtime._ensure_agent_watch_timer('group', 7)

        self.assertTrue(task['enabled'])
        self.assertNotIn('schedule_error', task)

    def test_blank_scope_creates_nothing(self):
        runtime = self._runtime()
        self.assertEqual(runtime._ensure_agent_watch_timer('', ''), '')
        self.assertEqual(runtime._ensure_agent_watch_timer('group', ''), '')
        self.assertEqual(runtime._recurring_tasks, {})

    def test_scope_id_zero_is_not_treated_as_blank(self):
        runtime = self._runtime()
        runtime._ensure_agent_watch_timer('group', 0)
        self.assertEqual(len(runtime._recurring_tasks), 1)

    # ── 待跟进快照 ────────────────────────────────────────────────
    def test_snapshot_filters_by_scope(self):
        runtime = self._runtime([_agent(scope='group:7'), _agent(agent_id='other', scope='group:8')])
        pending = runtime._scope_agent_snapshot('group:7')
        self.assertEqual([p['agent_id'] for p in pending], ['a1b2c3d4e5'])

    def test_idle_agents_are_not_pending(self):
        runtime = self._runtime([_agent(status='idle')])
        self.assertEqual(runtime._scope_agent_snapshot('group:7'), [])

    def test_waiting_review_error_all_count_as_pending(self):
        agents = [
            _agent(agent_id='w', status='waiting'),
            _agent(agent_id='r', status='review_required'),
            _agent(agent_id='e', status='error'),
        ]
        runtime = self._runtime(agents)
        self.assertEqual(len(runtime._scope_agent_snapshot('group:7')), 3)

    def test_snapshot_survives_manager_failure(self):
        runtime = self._runtime(raise_on_list=True)
        self.assertEqual(runtime._scope_agent_snapshot('group:7'), [])

    def test_snapshot_without_manager(self):
        runtime = object.__new__(AIOrchestrator)
        self.assertEqual(runtime._scope_agent_snapshot('group:7'), [])

    # ── 巡检文案 ──────────────────────────────────────────────────
    def test_instruction_lists_every_pending_agent(self):
        runtime = self._runtime([_agent(agent_id='aaaa1111bbbb'), _agent(agent_id='cccc2222dddd', status='waiting')])
        text = runtime._build_agent_watch_instruction('group:7')

        self.assertIn('2 个 agent', text)
        self.assertIn('aaaa1111', text)
        self.assertIn('cccc2222', text)
        # 合并汇报是硬要求，不能每个 agent 发一条
        self.assertIn('合并', text)

    def test_instruction_flags_stalled_agent(self):
        runtime = self._runtime([_agent(idle=1800)])
        text = runtime._build_agent_watch_instruction('group:7')
        self.assertIn('疑似卡住', text)
        self.assertIn('30分钟', text)

    def test_fresh_agent_is_not_flagged_stalled(self):
        runtime = self._runtime([_agent(idle=10)])
        self.assertNotIn('疑似卡住', runtime._build_agent_watch_instruction('group:7'))

    def test_instruction_is_none_when_nothing_pending(self):
        runtime = self._runtime([_agent(status='idle')])
        self.assertIsNone(runtime._build_agent_watch_instruction('group:7'))

    # ── 触发与自清理 ──────────────────────────────────────────────
    def test_trigger_injects_live_status_into_message(self):
        runtime = self._runtime([_agent(agent_id='aaaa1111bbbb')])
        runtime._ensure_agent_watch_timer('group', 7)
        task = next(iter(runtime._recurring_tasks.values()))

        submitted = []
        runtime._submit_message = submitted.append
        runtime._get_timed_event_messages = lambda: SimpleNamespace(
            build_recurring_message=lambda t: SimpleNamespace(text=t['instruction'])
        )

        self.assertTrue(runtime._trigger_recurring_task(task))
        self.assertEqual(len(submitted), 1)
        # 模型无需先 list_agents 就知道该看谁
        self.assertIn('aaaa1111', submitted[0].text)
        # 原任务记录不被改写
        self.assertNotIn('aaaa1111', task['instruction'])

    def test_trigger_deletes_timer_when_all_agents_done(self):
        runtime = self._runtime([_agent(status='idle')])
        runtime._ensure_agent_watch_timer('group', 7)
        task = next(iter(runtime._recurring_tasks.values()))
        runtime._submit_message = lambda _m: self.fail('无待跟进 agent 时不该再发巡检消息')

        self.assertTrue(runtime._trigger_recurring_task(task))
        self.assertEqual(runtime._recurring_tasks, {})

    def test_cleanup_only_touches_matching_scope(self):
        runtime = self._runtime()
        runtime._ensure_agent_watch_timer('group', 7)
        runtime._ensure_agent_watch_timer('group', 8)

        runtime._cleanup_agent_watch_timer('group:7')

        remaining = [t['target_scope'] for t in runtime._recurring_tasks.values()]
        self.assertEqual(remaining, ['group:8'])

    def test_cleanup_is_idempotent(self):
        runtime = self._runtime()
        runtime._cleanup_agent_watch_timer('group:7')
        self.assertEqual(runtime._recurring_tasks, {})

    def test_normal_recurring_task_is_untouched(self):
        runtime = self._runtime()
        task = {'id': 'x', 'instruction': '每天汇报', 'target_scope': 'group:7', 'enabled': True}
        runtime._recurring_tasks['x'] = task

        submitted = []
        runtime._submit_message = submitted.append
        runtime._get_timed_event_messages = lambda: SimpleNamespace(
            build_recurring_message=lambda t: SimpleNamespace(text=t['instruction'])
        )

        self.assertTrue(runtime._trigger_recurring_task(task))
        self.assertEqual(submitted[0].text, '每天汇报')
        self.assertIn('x', runtime._recurring_tasks)

    def test_trigger_reports_invalid_scope_as_failure(self):
        runtime = self._runtime([_agent()])
        runtime._ensure_agent_watch_timer('group', 7)
        task = next(iter(runtime._recurring_tasks.values()))
        runtime._get_timed_event_messages = lambda: SimpleNamespace(build_recurring_message=lambda _t: None)

        self.assertFalse(runtime._trigger_recurring_task(task))


if __name__ == '__main__':
    unittest.main()
