import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_runtime import AIOrchestrator, ScopeSendLedger


class ScopeSendLedgerTests(unittest.TestCase):
    def test_blocks_repeat_key_until_ttl_expires(self):
        ledger = ScopeSendLedger(ttl_seconds=300.0)
        ledger.add('k1')
        self.assertIn('k1', ledger)

    def test_allows_key_again_after_ttl(self):
        ledger = ScopeSendLedger(ttl_seconds=0.0)
        ledger.add('k1')
        # TTL 为 0 时立即过期：正常复读不该被永久封死
        self.assertNotIn('k1', ledger)

    def test_discard_releases_reservation(self):
        ledger = ScopeSendLedger(ttl_seconds=300.0)
        ledger.add('k1')
        ledger.discard('k1')
        self.assertNotIn('k1', ledger)

    def test_ledger_is_reused_per_scope_across_calls(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_key = lambda scope_type, scope_id: f'{scope_type}:{scope_id}'

        first = runtime._get_scope_send_ledger('group', '7')
        second = runtime._get_scope_send_ledger('group', '7')
        other = runtime._get_scope_send_ledger('group', '8')

        # 同 scope 必须拿到同一个账本，否则跨回合重复发送拦不住
        self.assertIs(first, second)
        self.assertIsNot(first, other)


class MentionSelfVisibilityTests(unittest.TestCase):
    def _runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(self_id=10001)
        return runtime

    def test_group_mention_is_marked_in_model_visible_text(self):
        runtime = self._runtime()
        message = SimpleNamespace(chat_type='group', mentions_self=True)

        marked = runtime._mark_mentions_self(message, '在吗')

        self.assertIn('@了你', marked)
        self.assertIn('在吗', marked)

    def test_group_message_without_mention_is_untouched(self):
        runtime = self._runtime()
        message = SimpleNamespace(chat_type='group', mentions_self=False)

        self.assertEqual(runtime._mark_mentions_self(message, '在吗'), '在吗')

    def test_private_message_is_untouched(self):
        runtime = self._runtime()
        message = SimpleNamespace(chat_type='private', mentions_self=True)

        self.assertEqual(runtime._mark_mentions_self(message, '在吗'), '在吗')

    def test_mark_is_not_applied_twice(self):
        runtime = self._runtime()
        message = SimpleNamespace(chat_type='group', mentions_self=True)

        once = runtime._mark_mentions_self(message, '在吗')
        twice = runtime._mark_mentions_self(message, once)

        self.assertEqual(once, twice)

    def test_trigger_entry_carries_mention_mark(self):
        runtime = self._runtime()
        runtime._normalize_message_ref = lambda _ref: ''
        runtime._annotate_message_refs = lambda _t, _i, entries: (entries, {})
        message = SimpleNamespace(
            chat_type='group',
            chat_id=7,
            mentions_self=True,
            user_id=222,
            nickname='阿白',
            text='@bot 在吗',
            message_id=9,
            images=[],
            raw_message='[CQ:at,qq=10001] 在吗',
            timestamp=1700000000,
            raw_data={},
        )

        entry = runtime._build_trigger_message_entry(message, '在吗')

        # 模型只看 text 字段，被 @ 这件事必须出现在这里
        self.assertIn('@了你', entry['text'])


if __name__ == '__main__':
    unittest.main()
