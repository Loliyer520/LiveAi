import asyncio
import unittest

from core.ai_runtime import AIOrchestrator


class MessageTurnCompletionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, process_message):
        runtime = object.__new__(AIOrchestrator)
        runtime._message_epoch = 5
        runtime._process_message = process_message
        runtime._is_epoch_stale = lambda epoch: int(epoch) != runtime._message_epoch
        runtime.merge_calls = []

        def merge_followup(item, completed):
            runtime.merge_calls.append((item, completed))
            return {'completed': completed}

        runtime._merge_followup_after_turn = merge_followup
        return runtime

    async def test_explicit_completion_evidence_selects_batch_merge(self):
        async def complete(item):
            item['followup_history_seed'] = [{'role': 'assistant', 'content': 'committed'}]
            item['turn_commit_evidence'] = {
                'outbound_history_committed': True,
                'turn_log_committed': True,
                'turn_metadata_committed': True,
            }

        runtime = self.runtime(complete)
        item = {'message_epoch': 5, 'scope_key': 'group:7'}
        followup = await runtime._run_message_turn(item)
        self.assertEqual(runtime.merge_calls, [(item, True)])
        self.assertEqual(followup, {'completed': True})

    async def test_missing_history_seed_does_not_claim_completion(self):
        async def incomplete(item):
            item['turn_commit_evidence'] = {
                'outbound_history_committed': True,
                'turn_log_committed': True,
                'turn_metadata_committed': True,
            }

        runtime = self.runtime(incomplete)
        item = {'message_epoch': 5, 'scope_key': 'group:7'}
        followup = await runtime._run_message_turn(item)
        self.assertEqual(runtime.merge_calls, [(item, False)])
        self.assertEqual(followup, {'completed': False})

    async def test_empty_disabled_or_finalize_early_return_does_not_claim_completion(self):
        async def early_return(_item):
            return None

        runtime = self.runtime(early_return)
        item = {'message_epoch': 5, 'scope_key': 'group:7'}
        followup = await runtime._run_message_turn(item)
        self.assertEqual(runtime.merge_calls, [(item, False)])
        self.assertEqual(followup, {'completed': False})

    async def test_stale_completed_turn_does_not_claim_completion(self):
        async def complete(item):
            item['followup_history_seed'] = [{'role': 'assistant', 'content': 'committed'}]
            item['turn_commit_evidence'] = {
                'outbound_history_committed': True,
                'turn_log_committed': True,
                'turn_metadata_committed': True,
            }
            runtime._message_epoch = 6

        runtime = self.runtime(complete)
        item = {'message_epoch': 5, 'scope_key': 'group:7'}
        followup = await runtime._run_message_turn(item)
        self.assertEqual(runtime.merge_calls, [(item, False)])
        self.assertEqual(followup, {'completed': False})

    async def test_exception_does_not_claim_completion(self):
        async def fail(_item):
            raise RuntimeError('failed before commit')

        runtime = self.runtime(fail)
        item = {'message_epoch': 5, 'scope_key': 'group:7'}
        followup = await runtime._run_message_turn(item)
        self.assertEqual(runtime.merge_calls, [(item, False)])
        self.assertEqual(followup, {'completed': False})

    async def test_real_cancelled_error_propagates_without_merging(self):
        async def cancel(_item):
            raise asyncio.CancelledError()

        runtime = self.runtime(cancel)
        item = {'message_epoch': 5, 'scope_key': 'group:7'}
        with self.assertRaises(asyncio.CancelledError):
            await runtime._run_message_turn(item)
        self.assertEqual(runtime.merge_calls, [])


if __name__ == '__main__':
    unittest.main()
