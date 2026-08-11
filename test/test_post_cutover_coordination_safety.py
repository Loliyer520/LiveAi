import inspect
import unittest

from core.ai_runtime import AIOrchestrator
from core.scope_actor_dispatcher import ScopeActorDispatcher


class PostCutoverCoordinationSafetyTests(unittest.TestCase):
    def test_dispatcher_is_runtime_owner_and_submission_is_encapsulated(self):
        source = inspect.getsource(AIOrchestrator)
        initializer = inspect.getsource(AIOrchestrator.__init__)
        enqueue_message = inspect.getsource(AIOrchestrator._enqueue_message)
        route_tasks = inspect.getsource(AIOrchestrator._route_task_queue_drain)

        self.assertNotIn('_pending_scope_turns', source)
        self.assertIn('ScopeActorDispatcher(', initializer)
        self.assertIn('consume=self._consume_scope_item', initializer)
        self.assertLess(
            initializer.index('self.agent_manager = AgentManager('),
            initializer.index('self.agent_manager.set_blocking_runner('),
        )
        self.assertIn('self._scope_dispatcher.submit_event(', enqueue_message)
        self.assertNotIn('_event_mailbox', enqueue_message)
        self.assertIn('await self._task_ingress_router.run()', route_tasks)
        self.assertNotIn('self.repo.get_task', route_tasks)
        self.assertNotIn('self._scope_dispatcher.submit_task', route_tasks)
        self.assertNotIn('_event_mailbox', route_tasks)

    def test_model_calls_use_completion_service_boundary(self):
        complete_chat = inspect.getsource(AIOrchestrator._complete_chat)
        snapshot_helper = inspect.getsource(AIOrchestrator._snapshot_for_role)
        self.assertIn('request_snapshot = self._snapshot_for_role(role, current)', complete_chat)
        self.assertIn('await self._model_completion.complete(', complete_chat)
        # main 角色沿用单例快照；tiered 子渠道在 helper 内构建独立客户端快照
        self.assertIn('self._model_completion.snapshot()', snapshot_helper)
        self.assertNotIn('model_client.complete', complete_chat)
        self.assertNotIn('self.model.complete', complete_chat)

    def test_actor_runs_followups_locally_and_task_finally_wakes_dispatcher(self):
        message_runner = inspect.getsource(AIOrchestrator._run_message_turn)
        actor_consumer = inspect.getsource(AIOrchestrator._consume_scope_item)
        process_task = inspect.getsource(AIOrchestrator._process_task)
        release_task = inspect.getsource(AIOrchestrator._release_task_scope)

        self.assertIn('self._has_completed_turn_commit(item)', message_runner)
        self.assertIn('return self._merge_followup_after_turn(item, completed)', message_runner)
        self.assertIn('while followup is not None:', actor_consumer)
        self.assertIn('followup = await self._run_message_turn(followup)', actor_consumer)
        self.assertNotIn('submit_event', actor_consumer)
        self.assertIn('finally:', process_task)
        self.assertIn('self._release_task_scope(scope_key)', process_task)
        self.assertIn('self._scope_dispatcher.wake(scope_key)', release_task)

    def test_followup_and_callback_tasks_reenter_via_task_ingress(self):
        submit_runtime_task = inspect.getsource(AIOrchestrator._submit_runtime_task)
        notify_master = inspect.getsource(AIOrchestrator._handle_notify_master)
        query_status = inspect.getsource(AIOrchestrator._handle_query_contact_status)
        child_report = inspect.getsource(AIOrchestrator._handle_child_report)
        report_child = inspect.getsource(AIOrchestrator._report_child_result)

        self.assertIn("self.queue.put_nowait({", submit_runtime_task)
        self.assertIn('self._submit_runtime_task(child_task.task_id)', notify_master)
        self.assertIn('self._submit_runtime_task(followup_task.task_id)', notify_master)
        self.assertNotIn("await self._process_task({'task_id': child_task.task_id})", notify_master)
        self.assertNotIn("await self._process_task({'task_id': followup_task.task_id})", notify_master)
        self.assertIn('self._submit_runtime_task(callback_task.task_id)', query_status)
        self.assertIn('self._submit_runtime_task(callback_task.task_id)', child_report)
        self.assertIn('self._submit_runtime_task(report_task.task_id)', report_child)

    def test_dispatcher_prioritizes_mailbox_before_pending_tasks(self):
        next_item = inspect.getsource(ScopeActorDispatcher._next_item)
        self.assertLess(
            next_item.index('self.mailbox.pop_scope_entry(scope_key)'),
            next_item.index('self.sessions.promote_pending_task_if_mailbox_empty(scope_key)'),
        )

    def test_tool_loop_drains_followup_via_unified_batch_coordinator(self):
        """工具循环必须经统一 batch 协调器摄入 followup，禁止旁路批量 drain。"""
        complete_turn = inspect.getsource(AIOrchestrator._complete_child_turn)
        drain_helper = inspect.getsource(AIOrchestrator._drain_live_tool_scope_turn)
        self.assertEqual(complete_turn.count('self._drain_live_tool_scope_turn(scope_key)'), 2)
        self.assertNotIn('pending = self._pop_pending_scope_turn(scope_key)', complete_turn)
        self.assertNotIn('_pop_next_live_pending_scope_turn', complete_turn)
        # mid-turn 与 post-turn 使用同一套 batch 协调器，保证 FIFO 批量摄入规则一致
        self.assertIn('drain_scope_followup(', drain_helper)
        self.assertIn('_turn_batch_coordinator.drain_scope_followup(', drain_helper)

    def test_agent_report_debounce_and_status_use_existing_coordination_contracts(self):
        reports = inspect.getsource(AIOrchestrator._flush_agent_reports)
        delivery = inspect.getsource(AIOrchestrator._deliver_agent_reports_to_scope)
        debounce = inspect.getsource(AIOrchestrator._fire_group_reply_trigger)
        status_command = inspect.getsource(AIOrchestrator._build_status_text)
        self.assertIn('self._get_agent_report_delivery().flush(', reports)
        self.assertIn('is_scope_active=self._scope_turn_is_active', reports)
        self.assertNotIn('_scope_turn_is_busy', reports)
        self.assertIn('self._submit_message(message)', delivery)
        self.assertNotIn('_event_mailbox', delivery)
        self.assertIn('self._scope_turn_is_busy(scope_key)', debounce)
        self.assertIn('self._pending_scope_turn_count(scope_key)', status_command)

    def test_alarm_and_recurring_paths_do_not_bypass_scope_submission(self):
        alarm = inspect.getsource(AIOrchestrator._alarm_runner)
        recurring = inspect.getsource(AIOrchestrator._trigger_recurring_task)
        self.assertNotIn('_event_mailbox', alarm)
        self.assertNotIn('_event_mailbox', recurring)
        self.assertNotIn('_notify_scope', alarm)
        self.assertIn('self._submit_message(message)', alarm)
        self.assertIn('build_alarm_message(', alarm)
        self.assertIn('self._submit_message(message)', recurring)
        self.assertIn('build_recurring_message(task)', recurring)


if __name__ == '__main__':
    unittest.main()
