import threading
import unittest
from unittest.mock import MagicMock, patch

from core.model_manager import ModelManager
from core.model_validation_service import ModelValidationService
from pack.anthropic_chat_model import AnthropicChatModel, AnthropicReply


# ─────────────────────────── ModelManager resolution ───────────────────────────

class ModelManagerResolutionTests(unittest.TestCase):
    def _make_manager(self):
        mm = object.__new__(ModelManager)
        mm._rr_counters = {}
        mm._fb_indexes = {}
        mm._request_fb_indexes = {}
        mm.config = {
            'upstreams': [
                {'name': 'up-a', 'base_url': 'https://a.example', 'api_key': 'key-a', 'messages_path': '/v1/messages'},
                {'name': 'up-b', 'base_url': 'https://b.example', 'api_key': 'key-b', 'messages_path': '/v1/chat/completions'},
            ],
            'channels': [
                {'name': 'ch1', 'strategy': 'fallback', 'models': [
                    {'upstream': 'up-a', 'model_id': 'model-x'},
                    {'upstream': 'up-b', 'model_id': 'model-y'},
                ]},
                {'name': 'ch2', 'strategy': 'fallback', 'models': [
                    {'upstream': 'up-a', 'model_id': 'model-x'},
                ]},
            ],
            'roles': {'main': 'ch1', 'agent': 'ch2'},
        }
        return mm

    def test_get_role_channel_name_returns_correct_channel(self):
        mm = self._make_manager()
        self.assertEqual(mm.get_role_channel_name('main'), 'ch1')
        self.assertEqual(mm.get_role_channel_name('agent'), 'ch2')

    def test_get_role_channel_name_falls_back_to_main(self):
        mm = self._make_manager()
        self.assertEqual(mm.get_role_channel_name('vision'), 'ch1')

    def test_get_role_channel_name_returns_none_when_no_main(self):
        mm = self._make_manager()
        mm.config['roles'] = {}
        self.assertIsNone(mm.get_role_channel_name('vision'))

    def test_resolve_channel_models_returns_all_entries(self):
        mm = self._make_manager()
        results = mm.resolve_channel_models('ch1')
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['upstream_name'], 'up-a')
        self.assertEqual(results[0]['model_name'], 'model-x')
        self.assertEqual(results[1]['upstream_name'], 'up-b')
        self.assertEqual(results[1]['model_name'], 'model-y')

    def test_resolve_channel_models_does_not_modify_fb_index(self):
        mm = self._make_manager()
        mm._fb_indexes['ch1'] = 1
        mm.resolve_channel_models('ch1')
        self.assertEqual(mm._fb_indexes['ch1'], 1)

    def test_resolve_channel_models_does_not_modify_rr_counter(self):
        mm = self._make_manager()
        mm._rr_counters['ch1'] = 5
        mm.resolve_channel_models('ch1')
        self.assertEqual(mm._rr_counters['ch1'], 5)

    def test_resolve_channel_models_unknown_channel_returns_empty(self):
        mm = self._make_manager()
        self.assertEqual(mm.resolve_channel_models('nonexistent'), [])

    def test_resolve_exact_model_matches_correctly(self):
        mm = self._make_manager()
        result = mm.resolve_exact_model('ch1', 'up-b', 'model-y')
        self.assertIsNotNone(result)
        self.assertEqual(result['upstream_name'], 'up-b')
        self.assertEqual(result['model_name'], 'model-y')

    def test_resolve_exact_model_wrong_upstream_returns_none(self):
        mm = self._make_manager()
        self.assertIsNone(mm.resolve_exact_model('ch1', 'up-a', 'model-y'))

    def test_resolve_exact_model_wrong_channel_returns_none(self):
        mm = self._make_manager()
        self.assertIsNone(mm.resolve_exact_model('ch2', 'up-b', 'model-y'))

    def test_resolve_exact_model_does_not_modify_state(self):
        mm = self._make_manager()
        mm._fb_indexes['ch1'] = 0
        mm.resolve_exact_model('ch1', 'up-a', 'model-x')
        self.assertEqual(mm._fb_indexes.get('ch1', 0), 0)

    def test_result_contains_no_api_key_in_display_name(self):
        mm = self._make_manager()
        result = mm.resolve_exact_model('ch1', 'up-a', 'model-x')
        self.assertNotIn('key-a', result.get('display_name', ''))

    def test_fallback_reset_restarts_from_first_each_request(self):
        mm = self._make_manager()
        mm.config['channels'][0]['strategy'] = 'fallback_reset'
        mm.begin_request('main')
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'model-x')
        mm.notify_failure('main')
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'model-y')
        mm.begin_request('main')
        self.assertEqual(mm.get_model_for_role('main')['model_name'], 'model-x')


# ─────────────────────────── ModelValidationService ───────────────────────────

class ModelValidationServiceTests(unittest.TestCase):
    def _make_service(self, probe_result):
        mm = MagicMock()
        mm.resolve_exact_model.return_value = {
            'base_url': 'https://a.example',
            'api_key': 'key-a',
            'model_name': 'model-x',
            'messages_path': '/v1/messages',
            'display_name': 'up-a/model-x',
            'channel_name': 'ch1',
            'upstream_name': 'up-a',
        }
        mm.resolve_channel_models.return_value = [
            {
                'base_url': 'https://a.example',
                'api_key': 'key-a',
                'model_name': 'model-x',
                'messages_path': '/v1/messages',
                'display_name': 'up-a/model-x',
                'channel_name': 'ch1',
                'upstream_name': 'up-a',
            }
        ]
        svc = ModelValidationService(mm)
        svc._probe = lambda cfg: probe_result
        return svc

    def test_validate_model_not_configured_returns_error(self):
        mm = MagicMock()
        mm.resolve_exact_model.return_value = None
        svc = ModelValidationService(mm)
        result = svc.validate_model('ch1', 'up-a', 'model-x')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'not_configured')

    def test_validate_model_success(self):
        mm = MagicMock()
        mm.resolve_exact_model.return_value = {
            'base_url': 'https://a.example',
            'api_key': 'key-a',
            'model_name': 'model-x',
            'messages_path': '/v1/messages',
            'display_name': 'up-a/model-x',
            'channel_name': 'ch1',
            'upstream_name': 'up-a',
        }
        svc = ModelValidationService(mm)
        with unittest.mock.patch('core.model_validation_service._probe', return_value={
            'ok': True, 'channel': 'ch1', 'upstream': 'up-a', 'model_id': 'model-x',
            'elapsed_ms': 100, 'error': None, 'display_name': 'up-a/model-x',
        }):
            result = svc.validate_model('ch1', 'up-a', 'model-x')
        self.assertTrue(result['ok'])

    def test_validate_channel_empty_returns_channel_not_found(self):
        mm = MagicMock()
        mm.resolve_channel_models.return_value = []
        svc = ModelValidationService(mm)
        results = svc.validate_channel('ch1')
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['ok'])
        self.assertEqual(results[0]['error'], 'channel_not_found')

    def test_validate_channel_aggregates_results(self):
        mm = MagicMock()
        mm.resolve_channel_models.return_value = [
            {'base_url': 'https://a.example', 'api_key': 'k', 'model_name': 'm1', 'messages_path': '/v1/messages', 'display_name': 'u/m1', 'channel_name': 'ch1', 'upstream_name': 'u'},
            {'base_url': 'https://b.example', 'api_key': 'k', 'model_name': 'm2', 'messages_path': '/v1/messages', 'display_name': 'u/m2', 'channel_name': 'ch1', 'upstream_name': 'u'},
        ]
        svc = ModelValidationService(mm)
        call_count = [0]
        def fake_probe(cfg):
            call_count[0] += 1
            return {'ok': True, 'channel': 'ch1', 'upstream': 'u', 'model_id': cfg['model_name'], 'elapsed_ms': 50, 'error': None, 'display_name': cfg['display_name']}
        with unittest.mock.patch('core.model_validation_service._probe', side_effect=fake_probe):
            results = svc.validate_channel('ch1')
        self.assertEqual(len(results), 2)
        self.assertEqual(call_count[0], 2)

    def test_result_does_not_contain_api_key(self):
        mm = MagicMock()
        mm.resolve_exact_model.return_value = {
            'base_url': 'https://a.example', 'api_key': 'secret-key-xyz',
            'model_name': 'model-x', 'messages_path': '/v1/messages',
            'display_name': 'up-a/model-x', 'channel_name': 'ch1', 'upstream_name': 'up-a',
        }
        svc = ModelValidationService(mm)
        captured = {}
        def fake_probe(cfg):
            captured['cfg'] = cfg
            return {'ok': True, 'channel': 'ch1', 'upstream': 'up-a', 'model_id': 'model-x', 'elapsed_ms': 10, 'error': None, 'display_name': 'up-a/model-x'}
        svc._probe = fake_probe
        result = svc.validate_model('ch1', 'up-a', 'model-x')
        result_str = str(result)
        self.assertNotIn('secret-key-xyz', result_str)


# ─────────────────────────── AnthropicChatModel request_timeout ───────────────

class AnthropicChatModelTimeoutTests(unittest.TestCase):
    def test_default_timeout_is_120(self):
        m = AnthropicChatModel('https://example.com')
        self.assertEqual(m.request_timeout, 120)

    def test_custom_timeout_is_preserved(self):
        m = AnthropicChatModel('https://example.com', request_timeout=15)
        self.assertEqual(m.request_timeout, 15)

    def test_with_config_preserves_timeout(self):
        m = AnthropicChatModel('https://example.com', request_timeout=30)
        m2 = m.with_config(model_name='other')
        self.assertEqual(m2.request_timeout, 30)

    def test_with_config_overrides_timeout(self):
        m = AnthropicChatModel('https://example.com', request_timeout=30)
        m2 = m.with_config(request_timeout=10)
        self.assertEqual(m2.request_timeout, 10)


# ─────────────────────────── _complete_chat timeout retry ─────────────────────

class TimeoutRetryTests(unittest.IsolatedAsyncioTestCase):
    def _make_runtime(self):
        from core.ai_runtime import AIOrchestrator
        from core.async_execution import AsyncExecutionPool
        from core.model_completion_service import ModelCompletionService

        rt = object.__new__(AIOrchestrator)
        rt._scope_retry次数 = {}
        rt._scope_current_model = {}
        rt._scope_thinking_levels = {}
        rt.token_usage_store = MagicMock()
        rt.token_usage_store.record = MagicMock()

        client = AnthropicChatModel('https://example.com', model_name='test-model')
        pool = AsyncExecutionPool('test-timeout-pool', 1)
        rt._model_completion = ModelCompletionService(
            get_client=lambda: client,
            default_pool=pool,
        )
        rt._pool = pool
        rt._mm_client = client

        mm = MagicMock()
        mm.get_current_model.return_value = {'display_name': 'test-model'}
        mm.begin_request = MagicMock()
        mm.notify_failure = MagicMock()
        rt.model_manager = mm
        rt._update_model_from_config = MagicMock()
        return rt, pool

    async def asyncTearDown(self):
        pass

    async def test_first_timeout_retries_same_model_no_notify_failure(self):
        rt, pool = self._make_runtime()
        calls = [0]

        async def fake_complete(snapshot, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                import requests
                raise requests.exceptions.Timeout('timed out')
            return AnthropicReply(text='ok')

        rt._model_completion.complete = fake_complete

        with patch('core.ai_runtime.asyncio.sleep', return_value=None):
            reply = await rt._complete_chat([], [], scope_key='group:1')

        self.assertEqual(calls[0], 2)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.text, 'ok')
        rt.model_manager.begin_request.assert_called_once_with('main')
        rt.model_manager.notify_failure.assert_not_called()

    async def test_second_timeout_advances_fallback(self):
        rt, pool = self._make_runtime()
        calls = [0]

        async def fake_complete(snapshot, *args, **kwargs):
            calls[0] += 1
            import requests
            raise requests.exceptions.Timeout('timed out')

        rt._model_completion.complete = fake_complete

        with patch('core.ai_runtime.asyncio.sleep', return_value=None):
            try:
                await rt._complete_chat([], [], scope_key='group:1')
            except Exception:
                pass

        self.assertGreaterEqual(rt.model_manager.notify_failure.call_count, 1)

    async def test_timeout_exhaustion_raises_original_error_instead_of_unboundlocal(self):
        rt, pool = self._make_runtime()

        async def fake_complete(snapshot, *args, **kwargs):
            import requests
            raise requests.exceptions.Timeout('timed out')

        rt._model_completion.complete = fake_complete

        with patch('core.ai_runtime.asyncio.sleep', return_value=None):
            with self.assertRaisesRegex(Exception, 'timed out'):
                await rt._complete_chat([], [], scope_key='group:1')

    async def test_5xx_immediately_advances_fallback_no_same_model_retry(self):
        rt, pool = self._make_runtime()
        calls = [0]

        async def fake_complete(snapshot, *args, **kwargs):
            calls[0] += 1
            raise RuntimeError('anthropic request failed status=500 body=err')

        rt._model_completion.complete = fake_complete

        with patch('core.ai_runtime.asyncio.sleep', return_value=None):
            try:
                await rt._complete_chat([], [], scope_key='group:1')
            except Exception:
                pass

        rt.model_manager.notify_failure.assert_called()
        # 5xx 不做同模型重试，每个 fallback 候选各调用一次
        self.assertEqual(calls[0], 3)

    async def test_5xx_exhaustion_raises_original_error_instead_of_unboundlocal(self):
        rt, pool = self._make_runtime()

        async def fake_complete(snapshot, *args, **kwargs):
            raise RuntimeError('anthropic request failed status=500 body=err')

        rt._model_completion.complete = fake_complete

        with patch('core.ai_runtime.asyncio.sleep', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'status=500'):
                await rt._complete_chat([], [], scope_key='group:1')

    async def test_connection_reset_advances_fallback_not_immediate_abort(self):
        rt, pool = self._make_runtime()
        calls = [0]

        async def fake_complete(snapshot, *args, **kwargs):
            calls[0] += 1
            raise ConnectionResetError('Connection reset by peer')

        rt._model_completion.complete = fake_complete

        with patch('core.ai_runtime.asyncio.sleep', return_value=None):
            with self.assertRaisesRegex(Exception, 'Connection reset by peer'):
                await rt._complete_chat([], [], scope_key='group:1')

        # 连接重置属于可 fallback 的瞬态错误：推进 fallback（每个候选各一次），
        # 而不是像非可重试错误那样立即 abort（只调用 1 次、notify_failure 不触发）。
        rt.model_manager.notify_failure.assert_called()
        self.assertEqual(calls[0], 3)

    async def test_connection_reset_recovers_on_fallback_model(self):
        rt, pool = self._make_runtime()
        calls = [0]

        async def fake_complete(snapshot, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise ConnectionError('Connection aborted.')
            return AnthropicReply(text='recovered')

        rt._model_completion.complete = fake_complete

        with patch('core.ai_runtime.asyncio.sleep', return_value=None):
            reply = await rt._complete_chat([], [], scope_key='group:1')

        self.assertEqual(calls[0], 2)
        self.assertEqual(reply.text, 'recovered')
        rt.model_manager.notify_failure.assert_called_once()

    async def test_non_retryable_error_raises_immediately(self):
        rt, pool = self._make_runtime()

        async def fake_complete(snapshot, *args, **kwargs):
            raise ValueError('bad input')

        rt._model_completion.complete = fake_complete

        with self.assertRaises(ValueError):
            await rt._complete_chat([], [], scope_key='group:1')

        rt.model_manager.notify_failure.assert_not_called()

    async def test_success_on_first_attempt_no_retry(self):
        rt, pool = self._make_runtime()
        calls = [0]

        async def fake_complete(snapshot, *args, **kwargs):
            calls[0] += 1
            return AnthropicReply(text='hello')

        rt._model_completion.complete = fake_complete

        reply = await rt._complete_chat([], [], scope_key='group:1')

        self.assertEqual(calls[0], 1)
        self.assertEqual(reply.text, 'hello')
        rt.model_manager.notify_failure.assert_not_called()

    def __del__(self):
        try:
            self._pool.close()
        except Exception:
            pass


if __name__ == '__main__':
    unittest.main()
