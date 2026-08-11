"""分级 AI 三渠道（聊天/执行/决策）回归测试。

覆盖：
- _resolve_tiered_role 场景判定（决策 > 执行 > 聊天）；
- _complete_chat 按 role 解析模型：tiered 子渠道独立快照、main 沿用单例；
- ModelManager 回退链 tiered_* → tiered → main 与 set_role 校验。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.ai_runtime import AIOrchestrator
from core.model_manager import ModelManager
from pack.anthropic_chat_model import AnthropicReply


def _runtime():
    return object.__new__(AIOrchestrator)


class ResolveTieredRoleTests(unittest.TestCase):
    def test_pure_user_message_uses_chat_channel(self):
        runtime = _runtime()
        role = runtime._resolve_tiered_role({
            'turn_kind': 'message',
            'trigger_count': 1,
            'combined_trigger_chars': 42,
        })
        self.assertEqual(role, 'tiered_chat')

    def test_agent_message_uses_decision_channel(self):
        runtime = _runtime()
        role = runtime._resolve_tiered_role({
            'turn_kind': 'message',
            'trigger_count': 1,
            'combined_trigger_chars': 10,
            'has_agent_message': True,
        })
        self.assertEqual(role, 'tiered_decision')

    def test_trigger_count_five_uses_decision_channel(self):
        runtime = _runtime()
        role = runtime._resolve_tiered_role({
            'turn_kind': 'message',
            'trigger_count': 5,
            'combined_trigger_chars': 10,
        })
        self.assertEqual(role, 'tiered_decision')

    def test_trigger_count_below_five_keeps_chat(self):
        runtime = _runtime()
        role = runtime._resolve_tiered_role({
            'turn_kind': 'message',
            'trigger_count': 4,
            'combined_trigger_chars': 10,
        })
        self.assertEqual(role, 'tiered_chat')

    def test_combined_chars_above_300_uses_decision_channel(self):
        runtime = _runtime()
        role = runtime._resolve_tiered_role({
            'turn_kind': 'message',
            'trigger_count': 2,
            'combined_trigger_chars': 301,
        })
        self.assertEqual(role, 'tiered_decision')

    def test_combined_chars_exactly_300_keeps_chat(self):
        runtime = _runtime()
        role = runtime._resolve_tiered_role({
            'turn_kind': 'message',
            'trigger_count': 2,
            'combined_trigger_chars': 300,
        })
        self.assertEqual(role, 'tiered_chat')

    def test_delegate_and_intel_query_use_exec_channel(self):
        runtime = _runtime()
        for kind in ('delegate', 'intel_query'):
            self.assertEqual(
                runtime._resolve_tiered_role({'turn_kind': kind}),
                'tiered_exec',
                f'turn_kind={kind} 应走执行渠道',
            )

    def test_tool_checkpoint_resume_uses_exec_channel(self):
        runtime = _runtime()
        role = runtime._resolve_tiered_role({
            'turn_kind': 'message',
            'trigger_count': 1,
            'combined_trigger_chars': 10,
            'resumed_from_tool_turn': True,
        })
        self.assertEqual(role, 'tiered_exec')

    def test_empty_meta_defaults_to_chat(self):
        runtime = _runtime()
        self.assertEqual(runtime._resolve_tiered_role(None), 'tiered_chat')
        self.assertEqual(runtime._resolve_tiered_role({}), 'tiered_chat')

    def test_decision_wins_over_exec_and_chat(self):
        runtime = _runtime()
        role = runtime._resolve_tiered_role({
            'turn_kind': 'delegate',
            'trigger_count': 7,
            'resumed_from_tool_turn': True,
        })
        self.assertEqual(role, 'tiered_decision')


class CompleteChatRoleTests(unittest.IsolatedAsyncioTestCase):
    def _stub_model_manager(self, *, models_by_role):
        mm = Mock()
        mm.begin_request = Mock()
        mm.notify_failure = Mock()
        mm.get_current_model = Mock(return_value=models_by_role.get('main'))
        mm.get_model_for_role = Mock(side_effect=lambda role: models_by_role.get(role))
        return mm

    def _stub_runtime(self, models_by_role: dict):
        runtime = _runtime()
        runtime.model_manager = self._stub_model_manager(models_by_role=models_by_role)
        runtime._scope_retry次数 = {}
        runtime._scope_current_model = {}
        runtime._scope_thinking_levels = {}
        runtime.token_usage_store = SimpleNamespace(record=Mock())
        runtime._update_model_from_config = Mock()
        runtime._model_completion = SimpleNamespace(
            snapshot=Mock(side_effect=AssertionError('tiered 角色不应走单例快照')),
            complete=AsyncMock(),
        )
        return runtime

    def _base_config(self):
        return {
            'base_url': 'https://tiered.example',
            'api_key': 'secret',
            'model_name': 'tiered-model',
            'messages_path': '/v1/messages',
            'display_name': 'up/tiered-model',
        }

    @patch('core.ai_runtime.get_bot_logger')
    @patch('core.ai_runtime.error')
    @patch('core.ai_runtime.warn')
    @patch('core.ai_runtime.info')
    async def test_tiered_role_uses_own_model_and_skips_singleton_update(self, _i, _w, _e, _gb):
        cfg = self._base_config()
        runtime = self._stub_runtime({'tiered_chat': cfg})
        runtime._model_completion.complete.return_value = AnthropicReply(
            text='hello', input_tokens=10, output_tokens=5,
        )
        reply = await runtime._complete_chat(
            [], [{'role': 'user', 'content': 'hi'}], None, 0.85,
            scope_key='private:1', role='tiered_chat',
        )
        self.assertEqual(reply.text, 'hello')
        # 角色化 begin_request 与模型解析
        runtime.model_manager.begin_request.assert_called_once_with('tiered_chat')
        runtime.model_manager.get_model_for_role.assert_called_with('tiered_chat')
        # 请求快照使用 tiered 渠道自己的模型
        called = runtime._model_completion.complete.await_args
        self.assertEqual(called.args[0].client.model_name, 'tiered-model')
        self.assertEqual(called.args[0].model_name, 'tiered-model')
        # tiered 子渠道不触碰运行时单例
        runtime._update_model_from_config.assert_not_called()
        runtime.token_usage_store.record.assert_called_once()

    @patch('core.ai_runtime.get_bot_logger')
    @patch('core.ai_runtime.error')
    @patch('core.ai_runtime.warn')
    @patch('core.ai_runtime.info')
    async def test_tiered_fallback_notifies_role_without_singleton_update(self, _i, _w, _e, _gb):
        cfg = self._base_config()
        runtime = self._stub_runtime({'tiered_exec': cfg})

        async def flaky_then_ok(snapshot, *_args, **_kwargs):
            if not getattr(flaky_then_ok, 'failed', False):
                flaky_then_ok.failed = True
                raise RuntimeError('status=500 upstream boom')
            return AnthropicReply(text='ok', input_tokens=1, output_tokens=1)

        runtime._model_completion.complete = AsyncMock(side_effect=flaky_then_ok)
        reply = await runtime._complete_chat(
            [], [], None, 0.7, scope_key='private:1', role='tiered_exec',
        )
        self.assertEqual(reply.text, 'ok')
        # 失败推进 tiered_exec 渠道的 fallback 索引
        runtime.model_manager.notify_failure.assert_called_once_with('tiered_exec')
        # 并发安全：子渠道失败不回写全局单例
        runtime._update_model_from_config.assert_not_called()

    @patch('core.ai_runtime.get_bot_logger')
    @patch('core.ai_runtime.error')
    @patch('core.ai_runtime.warn')
    @patch('core.ai_runtime.info')
    async def test_main_role_keeps_singleton_behavior(self, _i, _w, _e, _gb):
        main_cfg = dict(self._base_config())
        main_cfg['model_name'] = 'main-model'
        runtime = _runtime()
        runtime.model_manager = self._stub_model_manager(models_by_role={'main': main_cfg})
        runtime._scope_retry次数 = {}
        runtime._scope_current_model = {}
        runtime._scope_thinking_levels = {}
        runtime.token_usage_store = SimpleNamespace(record=Mock())
        runtime._update_model_from_config = Mock()
        singleton_client = SimpleNamespace(
            model_name='singleton-model',
            base_url='https://singleton.example',
            messages_path='/v1/messages',
        )
        runtime._model_completion = SimpleNamespace(
            snapshot=Mock(return_value=SimpleNamespace(
                client=singleton_client,
                model_name=singleton_client.model_name,
                api_url='https://singleton.example/v1/messages',
            )),
            complete=AsyncMock(return_value=AnthropicReply(
                text='main reply', input_tokens=1, output_tokens=1,
            )),
        )
        reply = await runtime._complete_chat(
            [], [], None, 0.7, scope_key='master:global', role='main',
        )
        self.assertEqual(reply.text, 'main reply')
        # main 走单例快照，不新建 tiered 客户端
        called = runtime._model_completion.complete.await_args
        self.assertEqual(called.args[0].client.model_name, 'singleton-model')
        runtime.model_manager.begin_request.assert_called_once_with('main')


class ModelManagerTieredFallbackTests(unittest.TestCase):
    def _make_manager(self, roles: dict) -> ModelManager:
        config = {
            'upstreams': [
                {'name': 'up-main', 'base_url': 'https://a', 'api_key': 'k', 'messages_path': '/v1/messages'},
                {'name': 'up-tiered', 'base_url': 'https://b', 'api_key': 'k', 'messages_path': '/v1/messages'},
            ],
            'channels': [
                {'name': 'main-ch', 'strategy': 'fallback', 'models': [{'upstream': 'up-main', 'model_id': 'main-1'}]},
                {'name': 'tiered-ch', 'strategy': 'fallback', 'models': [{'upstream': 'up-tiered', 'model_id': 'tiered-1'}]},
            ],
            'roles': roles,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'models_config.json'
            path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            return ModelManager(str(path))

    def test_sub_roles_fallback_chain(self):
        mm = self._make_manager({'main': 'main-ch', 'tiered': 'tiered-ch'})
        # 子角色未单独配置 → 回退 tiered 渠道
        self.assertEqual(mm.get_role_channel_name('tiered_chat'), 'tiered-ch')
        self.assertEqual(mm.get_role_channel_name('tiered_exec'), 'tiered-ch')
        self.assertEqual(mm.get_role_channel_name('tiered_decision'), 'tiered-ch')
        self.assertEqual(mm.get_model_for_role('tiered_decision')['model_name'], 'tiered-1')

    def test_sub_roles_support_explicit_binding(self):
        mm = self._make_manager({
            'main': 'main-ch',
            'tiered': 'tiered-ch',
            'tiered_decision': 'main-ch',
        })
        self.assertEqual(mm.get_role_channel_name('tiered_chat'), 'tiered-ch')
        self.assertEqual(mm.get_role_channel_name('tiered_decision'), 'main-ch')

    def test_set_role_accepts_sub_roles(self):
        mm = self._make_manager({'main': 'main-ch'})
        ok, msg = mm.set_role('tiered_chat', 'tiered-ch')
        self.assertTrue(ok, msg)
        self.assertEqual(mm.get_role_channel_name('tiered_chat'), 'tiered-ch')

    def test_set_role_rejects_unknown_role(self):
        mm = self._make_manager({'main': 'main-ch'})
        ok, _msg = mm.set_role('tiered_fancy', 'main-ch')
        self.assertFalse(ok)

    def test_role_list_shows_sub_roles(self):
        mm = self._make_manager({'main': 'main-ch'})
        text = mm.list_roles_text()
        self.assertIn('tiered_chat', text)
        self.assertIn('tiered_exec', text)
        self.assertIn('tiered_decision', text)

    def test_role_list_shows_effective_channel_via_fallback(self):
        mm = self._make_manager({'main': 'main-ch', 'tiered': 'tiered-ch'})
        text = mm.list_roles_text()
        # 子角色未显式配置 → 显示回退链解析后的实际生效渠道
        self.assertIn('tiered_decision', text)
        self.assertIn('tiered-ch', text)
        self.assertIn('回退自 tiered', text)
        # main 显式绑定 → 该行无回退标注
        main_line = next(ln for ln in text.splitlines() if '(main):' in ln)
        self.assertIn('main-ch [up-main/main-1]', main_line)
        self.assertNotIn('回退自', main_line)

    def test_role_binding_detail_resolution(self):
        mm = self._make_manager({'main': 'main-ch', 'tiered': 'tiered-ch', 'tiered_decision': 'main-ch'})
        # 显式绑定
        self.assertEqual(mm._resolve_role_binding_detail('main'), ('main-ch', 'main-ch', ''))
        self.assertEqual(mm._resolve_role_binding_detail('tiered_decision'), ('main-ch', 'main-ch', ''))
        # 回退 tiered
        self.assertEqual(mm._resolve_role_binding_detail('tiered_chat'), ('', 'tiered-ch', 'tiered'))
        self.assertEqual(mm._resolve_role_binding_detail('tiered_exec'), ('', 'tiered-ch', 'tiered'))
        # 回退 main
        self.assertEqual(mm._resolve_role_binding_detail('agent'), ('', 'main-ch', 'main'))
        # 完全未配置
        mm2 = self._make_manager({})
        self.assertEqual(mm2._resolve_role_binding_detail('main'), ('', '', ''))
        self.assertEqual(mm2._resolve_role_binding_detail('tiered_chat'), ('', '', ''))

    def test_summary_text_includes_tiered_routes(self):
        mm = self._make_manager({'main': 'main-ch', 'tiered': 'tiered-ch'})
        text = mm.get_summary_text()
        self.assertIn('分级分流', text)
        self.assertIn('tiered_chat:up-tiered/tiered-1', text)
        self.assertIn('tiered_decision:up-tiered/tiered-1', text)


if __name__ == '__main__':
    unittest.main()
