"""回归：QQ 好友/群申请事件实时唤醒主 AI，及 qq_sync_contacts 联系人同步工具。

覆盖三处修复：
1. NapCat request 事件此前只进进程内缓存，AI runtime 完全感知不到新好友/加群
   申请 → 新增 on_friend_request / on_group_request 分发，runtime 包装成 master
   scope 内部通知消息走 mailbox，消除"申请到了但 AI 不知道"的同步空窗。
2. qq_sync_contacts 工具：主 AI 主动刷新好友/群列表并落库联系人身份。
3. 申请通知消息的来源识别为 internal_task，正常唤醒主 AI 而不被当作普通聊天。
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_runtime import AIOrchestrator
from core.events import ChatMessage


def _make_runtime():
    runtime = object.__new__(AIOrchestrator)
    runtime.bot = SimpleNamespace(self_id='1059681169')
    # handler 里 `if not self.loop or not self.queue: self.start()` 需绕过，
    # 用占位对象避免真实启动事件循环。
    runtime.loop = object()
    runtime.queue = object()
    runtime._submit_message = Mock()
    return runtime


def _event(**overrides):
    event = {
        'post_type': 'request',
        'request_type': 'friend',
        'user_id': 123456,
        'comment': '你好，我是小明',
        'flag': 'FRIEND_FLAG_1',
        'nickname': '小明',
    }
    event.update(overrides)
    return event


class FriendRequestEventWakesMasterTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _make_runtime()

    def test_friend_request_event_submits_master_message(self):
        """好友申请事件 → master scope 内部通知消息，mentions_self 强制触发主 AI。"""
        self.runtime.handle_friend_request(_event())

        self.runtime._submit_message.assert_called_once()
        message = self.runtime._submit_message.call_args.args[0]
        self.assertIsInstance(message, ChatMessage)
        self.assertEqual(message.chat_type, 'master')
        self.assertEqual(message.chat_id, 0)
        self.assertEqual(message.user_id, 0)
        self.assertTrue(message.mentions_self)
        self.assertEqual(message.raw_data['source'], 'qq_request_event')
        self.assertEqual(message.raw_data['request_type'], 'friend')
        self.assertEqual(message.raw_data['flag'], 'FRIEND_FLAG_1')
        self.assertIn('123456', message.text)
        self.assertIn('你好，我是小明', message.text)

    def test_group_request_event_submits_master_message(self):
        """加群申请事件 → master scope 通知，带群号与 sub_type。"""
        self.runtime.handle_group_request(_event(request_type='group', group_id=888888, sub_type='add'))

        message = self.runtime._submit_message.call_args.args[0]
        self.assertEqual(message.raw_data['request_type'], 'group')
        self.assertEqual(message.raw_data['group_id'], 888888)
        self.assertIn('加群申请', message.text)
        self.assertIn('888888', message.text)

    def test_invite_event_uses_invite_label(self):
        self.runtime.handle_group_request(_event(request_type='group', group_id=888888, sub_type='invite'))
        message = self.runtime._submit_message.call_args.args[0]
        self.assertIn('入群邀请', message.text)

    def test_self_request_event_is_ignored(self):
        """申请人是 bot 自身时跳过，避免自环。"""
        self.runtime.handle_friend_request(_event(user_id=self.runtime.bot.self_id))
        self.runtime._submit_message.assert_not_called()

    def test_request_event_source_kind_is_internal_task(self):
        """qq_request_event 来源必须识别为 internal_task，正常唤醒 AI。"""
        message = ChatMessage(
            chat_type='master',
            chat_id=0,
            user_id=0,
            text='【系统通知】收到新的好友申请',
            raw_message='【系统通知】收到新的好友申请',
            sender={'nickname': '系统通知', 'user_id': 0},
            message_id=None,
            mentions_self=True,
            timestamp=0,
            raw_data={'source': 'qq_request_event', 'request_type': 'friend'},
        )
        self.assertEqual(self.runtime._message_source_kind(message), 'internal_task')

    def test_master_scope_router_registration_wires_request_handlers(self):
        """register() 必须订阅好友/群申请事件。"""
        event_source = SimpleNamespace(
            on_group_message=Mock(),
            on_private_message=Mock(),
            on_self_message=Mock(),
            on_friend_request=Mock(),
            on_group_request=Mock(),
        )
        runtime = object.__new__(AIOrchestrator)
        runtime.event_source = event_source
        runtime.register()
        event_source.on_friend_request.assert_called_once()
        event_source.on_group_request.assert_called_once()


class QqSyncContactsToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = object.__new__(AIOrchestrator)
        self.runtime.config = SimpleNamespace(history_limit=100)
        self.runtime.tools = SimpleNamespace(
            get_friend_list=Mock(return_value=[
                {'user_id': 1001, 'nickname': '老友'},
                {'user_id': 1002, 'nickname': '新朋友'},
            ]),
            get_group_list=Mock(return_value=[
                {'group_id': 2001, 'group_name': '群A'},
            ]),
            record_tool_use=Mock(),
        )
        self.runtime.repo = SimpleNamespace(
            get_user_profile=Mock(side_effect=lambda uid: None if uid == '1002' else {'user_id': uid}),
            touch_user_identity=Mock(),
        )

    async def test_sync_contacts_lands_identities_and_reports_counts(self):
        result = await self.runtime._run_ai_tool_call('master', '0', 'master:0', 'qq_sync_contacts', {})

        self.assertIn('好友 2 个', result)
        self.assertIn('群 1 个', result)
        self.assertIn('本次新增联系人身份 1 个', result)
        # 两个好友都应落库为 private 身份
        self.runtime.repo.touch_user_identity.assert_any_call('1001', '老友', 'private', '1001')
        self.runtime.repo.touch_user_identity.assert_any_call('1002', '新朋友', 'private', '1002')

    async def test_sync_contacts_restricted_to_master(self):
        result = await self.runtime._run_ai_tool_call('private', '1001', 'private:1001', 'qq_sync_contacts', {})
        self.assertIn('仅允许主AI', result)
        self.runtime.repo.touch_user_identity.assert_not_called()


if __name__ == '__main__':
    unittest.main()
