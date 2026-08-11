"""/test 按角色测试（分级分流同步）回归测试。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.model_manager import ModelManager
from core.test_command import _cmd_test_role, _cmd_all_roles, _test_request, handle_test_command


def _make_manager(roles: dict) -> ModelManager:
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


class _Bot:
    def __init__(self):
        self.texts = []

    def send_text(self, _chat_type, _chat_id, text):
        self.texts.append(text)


def _msg():
    return type('M', (), {'chat_type': 'private', 'chat_id': '1'})()


class RoleTestCommandTests(unittest.IsolatedAsyncioTestCase):
    @patch('core.test_command._test_request', new_callable=AsyncMock)
    async def test_cmd_test_role_uses_effective_channel(self, mock_req):
        mm = _make_manager({'main': 'main-ch', 'tiered': 'tiered-ch'})
        mock_req.return_value = {'success': True, 'elapsed_ms': 123, 'status': 200}
        bot = _Bot()
        # tiered_decision 未显式配置 → 回退 tiered → tiered-ch
        await _cmd_test_role(mm, 'tiered_decision', bot, _msg())
        self.assertEqual(len(bot.texts), 1)
        self.assertIn('[OK]', bot.texts[0])
        self.assertIn('tiered-ch', bot.texts[0])
        self.assertIn('up-tiered/tiered-1', bot.texts[0])

    @patch('core.test_command._test_request', new_callable=AsyncMock)
    async def test_cmd_test_role_explicit_binding(self, mock_req):
        mm = _make_manager({'main': 'main-ch', 'tiered_decision': 'main-ch'})
        mock_req.return_value = {'success': True, 'elapsed_ms': 9, 'status': 200}
        bot = _Bot()
        await _cmd_test_role(mm, 'tiered_decision', bot, _msg())
        self.assertIn('main-ch', bot.texts[0])
        self.assertIn('up-main/main-1', bot.texts[0])

    async def test_cmd_test_role_unknown_role(self):
        mm = _make_manager({})
        bot = _Bot()
        await _cmd_test_role(mm, 'tiered_fancy', bot, _msg())
        self.assertIn('未知角色', bot.texts[0])

    async def test_cmd_test_role_unconfigured_role(self):
        mm = _make_manager({})
        bot = _Bot()
        await _cmd_test_role(mm, 'tiered_chat', bot, _msg())
        self.assertIn('未解析到渠道', bot.texts[0])

    @patch('core.test_command._test_request', new_callable=AsyncMock)
    async def test_cmd_all_roles_covers_tiered_sub_roles(self, mock_req):
        mm = _make_manager({'main': 'main-ch', 'tiered': 'tiered-ch'})
        mock_req.return_value = {'success': True, 'elapsed_ms': 5, 'status': 200}
        bot = _Bot()
        await _cmd_all_roles(mm, bot, _msg())
        text = '\n'.join(bot.texts)
        for role in ('main', 'tiered', 'tiered_chat', 'tiered_exec', 'tiered_decision',
                     'agent', 'tasker', 'vision'):
            self.assertIn(f'({role})', text, f'角色 {role} 应被覆盖')

    @patch('core.test_command._test_request', new_callable=AsyncMock)
    async def test_handle_test_command_role_routes(self, mock_req):
        mm = _make_manager({'main': 'main-ch', 'tiered': 'tiered-ch'})
        mock_req.return_value = {'success': True, 'elapsed_ms': 11, 'status': 200}
        bot = _Bot()
        await handle_test_command(_msg(), '/test role tiered_chat', mm, bot)
        self.assertEqual(len(bot.texts), 1)
        self.assertIn('tiered_chat', bot.texts[0])

    @patch('core.test_command._test_request', new_callable=AsyncMock)
    async def test_handle_test_command_roles_routes(self, mock_req):
        mm = _make_manager({'main': 'main-ch', 'tiered': 'tiered-ch'})
        mock_req.return_value = {'success': True, 'elapsed_ms': 3, 'status': 200}
        bot = _Bot()
        await handle_test_command(_msg(), '/test roles', mm, bot)
        text = '\n'.join(bot.texts)
        self.assertIn('tiered_decision', text)
        self.assertIn('tiered-ch', text)

    async def test_test_request_uses_responses_payload(self):
        response = type('R', (), {'status_code': 200, 'text': ''})()
        client = AsyncMock()
        client.post.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch('core.test_command._httpx.AsyncClient', return_value=context):
            result = await _test_request({
                'base_url': 'https://example.invalid',
                'api_key': 'key',
                'messages_path': '/v1/responses',
            }, 'gpt-test')
        self.assertTrue(result['success'])
        _args, kwargs = client.post.call_args
        self.assertEqual(kwargs['json']['input'][0]['content'], 'hi')
        self.assertEqual(kwargs['json']['max_output_tokens'], 10)
        self.assertNotIn('messages', kwargs['json'])
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer key')


if __name__ == '__main__':
    unittest.main()
