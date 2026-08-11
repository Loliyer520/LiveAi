import unittest
from dataclasses import FrozenInstanceError

from core.transport import ActionEnvelope, LegacyNapcatTransport
from tool.contact_tool import ContactTool


class RecordingBot:
    def __init__(self):
        self.self_id = 241898129
        self.calls = []
        self.callbacks = {}

    def __getattr__(self, name):
        if name.startswith('on_'):
            def register(callback):
                self.callbacks[name] = callback
            return register

        def call(**kwargs):
            self.calls.append((name, kwargs))
            return {'status': 'ok', 'data': {'method': name}}
        return call


class ActionEnvelopeContractTest(unittest.TestCase):
    def test_envelope_is_immutable_and_has_operation_id(self):
        action = ActionEnvelope('send_text', {'message': 'hello'}, 'group', 42)
        self.assertEqual(action.action_type, 'send_text')
        self.assertEqual(action.scope_type, 'group')
        self.assertEqual(action.scope_id, 42)
        self.assertTrue(action.operation_id)
        with self.assertRaises(FrozenInstanceError):
            action.action_type = 'send_image'

    def test_legacy_transport_preserves_core_action_arguments_and_results(self):
        bot = RecordingBot()
        transport = LegacyNapcatTransport(bot)

        result = transport.send_text('group', 100, 'hello')
        transport.send_image('private', 200, 'base64://abc', 'caption')
        transport.send_mface('group', 100, 'emo-1', 'pkg-2', 'key-3', '[表情包]')
        transport.send_file('group', 100, '/tmp/a.txt', 'a.txt')
        transport.recall_message(987)
        transport.set_group_ban(100, 200, 60)
        transport.set_group_whole_ban(100, True)
        transport.get_group_member_info(100, 200, True)

        self.assertEqual(result, {'status': 'ok', 'data': {'method': 'send_text'}})
        self.assertEqual(bot.calls, [
            ('send_text', {'chat_type': 'group', 'target_id': 100, 'message': 'hello'}),
            ('send_image', {'chat_type': 'private', 'target_id': 200, 'file': 'base64://abc', 'text': 'caption'}),
            ('send_mface', {'chat_type': 'group', 'target_id': 100, 'emoji_id': 'emo-1', 'emoji_package_id': 'pkg-2', 'key': 'key-3', 'summary': '[表情包]'}),
            ('send_file', {'chat_type': 'group', 'target_id': 100, 'file': '/tmp/a.txt', 'name': 'a.txt'}),
            ('recall_message', {'message_id': 987}),
            ('set_group_ban', {'group_id': 100, 'user_id': 200, 'duration': 60}),
            ('set_group_whole_ban', {'group_id': 100, 'enable': True}),
            ('get_group_member_info', {'group_id': 100, 'user_id': 200, 'no_cache': True}),
        ])

    def test_legacy_transport_preserves_queries_and_request_management(self):
        bot = RecordingBot()
        transport = LegacyNapcatTransport(bot)

        transport.get_group_list()
        transport.get_friend_list()
        transport.get_file('file-1')
        transport.fetch_custom_face(12)
        transport.get_friend_requests(10)
        transport.set_friend_add_request('friend-flag', True, 'remark')
        transport.get_group_requests(11)
        transport.set_group_add_request('group-flag', 'add', False, 'reason')

        self.assertEqual(bot.calls, [
            ('get_group_list', {}),
            ('get_friend_list', {}),
            ('get_file', {'file_id': 'file-1'}),
            ('fetch_custom_face', {'count': 12}),
            ('get_friend_requests', {'count': 10}),
            ('set_friend_add_request', {'flag': 'friend-flag', 'approve': True, 'remark': 'remark'}),
            ('get_group_requests', {'count': 11}),
            ('set_group_add_request', {'flag': 'group-flag', 'sub_type': 'add', 'approve': False, 'reason': 'reason'}),
        ])

    def test_inbound_registration_delegates_in_legacy_mode(self):
        bot = RecordingBot()
        transport = LegacyNapcatTransport(bot)
        callback = lambda event: event

        transport.on_group_message(callback)
        transport.on_private_message(callback)
        transport.on_self_message(callback)
        transport.on_group_increase(callback)

        self.assertEqual(set(bot.callbacks), {
            'on_group_message', 'on_private_message', 'on_self_message', 'on_group_increase'
        })
        self.assertTrue(all(value is callback for value in bot.callbacks.values()))

    def test_contact_tool_depends_only_on_transport_contract(self):
        bot = RecordingBot()
        contact = ContactTool(LegacyNapcatTransport(bot))

        contact.send_private_message(200, 'hello')
        contact.send_chat_image('group', 100, 'x.png', 'caption')
        contact.send_chat_file('private', 200, '/tmp/a.txt', 'a.txt')
        role = contact.get_member_role(100, 200)

        self.assertEqual(role, 'unknown')  # RecordingBot returns no role, matching legacy fallback.
        self.assertEqual(bot.calls[:3], [
            ('send_text', {'chat_type': 'private', 'target_id': 200, 'message': 'hello'}),
            ('send_image', {'chat_type': 'group', 'target_id': 100, 'file': 'x.png', 'text': 'caption'}),
            ('send_file', {'chat_type': 'private', 'target_id': 200, 'file': '/tmp/a.txt', 'name': 'a.txt'}),
        ])


if __name__ == '__main__':
    unittest.main()
