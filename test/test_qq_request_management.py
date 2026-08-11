import asyncio
import json
import unittest

from core.ai_runtime import AIOrchestrator
from core.ai_tools_schema import build_tools
from pack.napcat import NapcatBot


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {'status': 'ok', 'retcode': 0, 'data': None}


class QQRequestManagementTests(unittest.TestCase):
    def test_tools_only_registered_for_master(self):
        normal = {item['name'] for item in build_tools()}
        master = {item['name'] for item in build_tools(include_qq_request_management=True)}
        self.assertNotIn('qq_list_friend_requests', normal)
        self.assertIn('qq_list_friend_requests', master)
        self.assertIn('qq_reject_group_request', master)

    def test_request_event_cached_without_persistence(self):
        bot = NapcatBot('ws://invalid', 'http://invalid', 1)
        payload = {'post_type': 'request', 'request_type': 'friend', 'user_id': 2, 'flag': 'secret'}
        bot._on_message(None, json.dumps(payload))
        cached = bot.get_friend_requests(10)['event_cache']
        self.assertEqual(cached[0]['flag'], 'secret')

    def test_unsupported_active_operations_never_call_http(self):
        bot = NapcatBot('ws://invalid', 'http://invalid', 1)
        bot.post = lambda *args, **kwargs: self.fail('unsupported operation must not call HTTP')
        with self.assertRaises(NotImplementedError):
            bot.request_friend_add(2)
        with self.assertRaises(NotImplementedError):
            bot.request_group_join(3)

    def test_non_master_runtime_denied_before_toolbox(self):
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator.tools = type('Tools', (), {
            'set_friend_add_request': lambda *args, **kwargs: self.fail('must not execute'),
            'record_tool_use': lambda *args, **kwargs: None,
        })()
        orchestrator.config = type('Config', (), {'history_limit': 20})()
        result = asyncio.run(orchestrator._run_ai_tool_call(
            'private', '123', 'private:123', 'qq_approve_friend_request', {'flag': 'x'}
        ))
        self.assertIn('仅允许主AI', result)

    def test_onebot_business_error_is_not_success(self):
        with self.assertRaises(RuntimeError):
            NapcatBot._require_action_success('x', {'status': 'failed', 'retcode': 1404, 'message': 'unsupported'})

    def test_send_text_rejects_onebot_business_error(self):
        bot = NapcatBot('ws://invalid', 'http://invalid', 1)
        bot.post = lambda *_args, **_kwargs: {
            'status': 'failed',
            'retcode': 1404,
            'message': 'send blocked',
            'data': None,
        }

        with self.assertRaisesRegex(RuntimeError, 'retcode=1404'):
            bot.send_text('private', 2, 'hello')

        self.assertEqual(bot._recent_self_sent_ids, {})
        self.assertEqual(bot._pending_self_sent, {})

    def test_send_image_rejects_onebot_business_error(self):
        bot = NapcatBot('ws://invalid', 'http://invalid', 1)
        bot.post = lambda *_args, **_kwargs: {
            'status': 'failed',
            'retcode': 1404,
            'message': 'send blocked',
            'data': None,
        }

        with self.assertRaisesRegex(RuntimeError, 'retcode=1404'):
            bot.send_image('private', 2, 'base64://fake')

        self.assertEqual(bot._recent_self_sent_ids, {})
        self.assertEqual(bot._pending_self_sent, {})

    def test_send_mface_rejects_onebot_business_error(self):
        bot = NapcatBot('ws://invalid', 'http://invalid', 1)
        bot.post = lambda *_args, **_kwargs: {
            'status': 'failed',
            'retcode': 1404,
            'message': 'send blocked',
            'data': None,
        }

        with self.assertRaisesRegex(RuntimeError, 'retcode=1404'):
            bot.send_mface('private', 2, 'emo-1', 'pkg-2', 'key-3', '[表情包]')

        self.assertEqual(bot._recent_self_sent_ids, {})
        self.assertEqual(bot._pending_self_sent, {})


if __name__ == '__main__':
    unittest.main()
