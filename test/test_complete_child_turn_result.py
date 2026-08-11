import unittest

from core.ai_runtime import AIOrchestrator


class CompleteChildTurnResultContractTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        return object.__new__(AIOrchestrator)

    def test_uncommitted_result_has_no_metadata(self):
        runtime = self.runtime()
        bundle = runtime._turn_result_bundle(
            {'message': '', 'think_note': '', 'tool_context_messages': []},
            turn_log_committed=False,
            agent_id='agent-1',
            temperature=0.85,
            turn_meta={'turn_kind': 'message'},
            tool_iterations=[{'assistant_text': 'not-logged'}],
            generation_ms=10,
        )
        self.assertFalse(bundle['turn_log_committed'])
        self.assertIsNone(bundle['turn_metadata'])

    def test_committed_result_reuses_existing_turn_log_metadata(self):
        runtime = self.runtime()
        iterations = [{'assistant_text': 'logged', 'tool_calls': []}]
        turn_meta = {'turn_kind': 'message', 'trigger_messages': [{'text': 'hello'}]}
        bundle = runtime._turn_result_bundle(
            {'message': 'reply', 'think_note': 'note', 'tool_context_messages': []},
            turn_log_committed=True,
            agent_id='agent-1',
            temperature=0.85,
            turn_meta=turn_meta,
            tool_iterations=iterations,
            generation_ms=42,
            note='tool_loop_guard',
        )
        self.assertTrue(bundle['turn_log_committed'])
        self.assertEqual(bundle['turn_metadata']['agent_id'], 'agent-1')
        self.assertEqual(bundle['turn_metadata']['turn_meta'], turn_meta)
        self.assertEqual(bundle['turn_metadata']['tool_iterations'], iterations)
        self.assertEqual(bundle['turn_metadata']['generation_ms'], 42)
        self.assertEqual(bundle['turn_metadata']['note'], 'tool_loop_guard')
        iterations[0]['assistant_text'] = 'mutated'
        self.assertEqual(bundle['turn_metadata']['tool_iterations'][0]['assistant_text'], 'logged')

    def test_all_complete_child_turn_return_bundles_use_explicit_contract(self):
        import inspect

        source = inspect.getsource(AIOrchestrator._complete_child_turn)
        self.assertNotIn("return {'message':", source)
        self.assertEqual(source.count('return self._turn_result_bundle('), 10)

    def test_return_branch_commit_flags_match_record_turn_log_control_flow(self):
        import inspect

        source = inspect.getsource(AIOrchestrator._complete_child_turn)
        returns = [line.strip() for line in source.splitlines() if 'return self._turn_result_bundle(' in line]
        self.assertEqual(len(returns), 10)
        self.assertIn('turn_log_committed=False', returns[0])  # epoch stale before provider
        self.assertIn('turn_log_committed=False', returns[1])  # epoch stale after provider
        self.assertIn('turn_log_committed=True', returns[2])   # empty reply logged
        for line in returns[3:]:
            self.assertIn('turn_log_committed=True', line)

    async def test_empty_reply_dynamically_reports_logged_commit(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        runtime = self.runtime()
        runtime.config = SimpleNamespace(admin_qq=1, history_limit=10)
        runtime._static_system_blocks = lambda _text: []
        runtime._system_prompt = lambda: 'system'
        runtime._scope_key = lambda scope_type, scope_id: f'{scope_type}:{scope_id}'
        runtime._is_epoch_stale = lambda _epoch: False
        runtime._scope_session_modes = {}
        runtime._complete_chat = AsyncMock(return_value=None)
        runtime._record_turn_log = AsyncMock()
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'group', '7', 'agent-1', {'system': [], 'messages': []}, 0.85,
            run_epoch=1, turn_meta={'turn_kind': 'message'}, live_message=None,
        )
        runtime._record_turn_log.assert_awaited_once()
        self.assertTrue(bundle['turn_log_committed'])
        self.assertIsNotNone(bundle['turn_metadata'])

    async def test_epoch_stale_and_cancelled_error_never_report_commit(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import asyncio

        runtime = self.runtime()
        runtime.config = SimpleNamespace(admin_qq=1, history_limit=10)
        runtime._static_system_blocks = lambda _text: []
        runtime._system_prompt = lambda: 'system'
        runtime._scope_key = lambda scope_type, scope_id: f'{scope_type}:{scope_id}'
        runtime._is_epoch_stale = lambda _epoch: True
        runtime._scope_session_modes = {}
        runtime._complete_chat = AsyncMock()
        runtime._record_turn_log = AsyncMock()
        bundle, _generation_ms, _used_tools = await runtime._complete_child_turn(
            'group', '7', 'agent-1', {'system': [], 'messages': []}, 0.85,
            run_epoch=1, turn_meta={'turn_kind': 'message'}, live_message=None,
        )
        self.assertFalse(bundle['turn_log_committed'])
        runtime._record_turn_log.assert_not_awaited()

        runtime._is_epoch_stale = lambda _epoch: False
        runtime._complete_chat = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await runtime._complete_child_turn(
                'group', '7', 'agent-1', {'system': [], 'messages': []}, 0.85,
                run_epoch=1, turn_meta={'turn_kind': 'message'}, live_message=None,
            )
        runtime._record_turn_log.assert_not_awaited()
if __name__ == '__main__':
    unittest.main()
