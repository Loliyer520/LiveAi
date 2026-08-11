"""回归：AI 自我身份认知 —— 不得把号主 QQ 当成自己 QQ。

背景：prompt 文件（main.txt/staff/*/agent.txt/dev_agent.txt）只硬编码了号主
QQ（241898129），从未注入 bot 自身 QQ。AI 被告知"你是这个 QQ 机器人"却不知道
自己的 QQ 号，唯一见过的 QQ 数字就是号主号，被问"你的QQ号/这个账号"时会把
号主 QQ 当成自己。修复：运行时把 bot self_id 与号主 master_qq 动态注入
system prompt 与每轮消息上下文。
"""

import unittest
from types import SimpleNamespace

from core.ai_runtime import AIOrchestrator


def _make_runtime(bot_qq='1059681169', master_qq='241898129', admin_qq='241898129'):
    runtime = object.__new__(AIOrchestrator)
    runtime.bot = SimpleNamespace(self_id=bot_qq)
    runtime.config = SimpleNamespace(master_qq=master_qq, admin_qq=admin_qq, self_id='0')
    return runtime


class IdentityPromptInjectionTests(unittest.TestCase):
    def test_identity_block_contains_both_qq_and_distinguishes_them(self):
        block = _make_runtime()._identity_prompt_block()
        self.assertIn('1059681169', block)   # 自己的 QQ
        self.assertIn('241898129', block)    # 号主 QQ
        self.assertIn('两个不同账号', block)  # 明确区分
        self.assertIn('你的 QQ 号（机器人自身账号）', block)

    def test_system_prompt_appends_identity_block(self):
        runtime = _make_runtime()
        runtime.prompt_store = SimpleNamespace(staff_system_prompt=lambda: 'staff 基线')
        result = runtime._system_prompt()
        self.assertTrue(result.startswith('staff 基线'))
        self.assertIn('【身份基线】', result)
        self.assertIn('1059681169', result)

    def test_master_system_prompt_appends_identity_block(self):
        runtime = _make_runtime()
        runtime.prompt_store = SimpleNamespace(main_system_prompt=lambda: 'main 基线')
        result = runtime._master_system_prompt()
        self.assertTrue(result.startswith('main 基线'))
        self.assertIn('【身份基线】', result)
        self.assertIn('1059681169', result)

    def test_identity_block_falls_back_to_config_self_id(self):
        runtime = _make_runtime()
        runtime.bot = SimpleNamespace(self_id='0')
        runtime.config = SimpleNamespace(master_qq='241898129', admin_qq='241898129', self_id='1059681169')
        block = runtime._identity_prompt_block()
        self.assertIn('1059681169', block)

    def test_identity_block_empty_when_no_qq_known(self):
        runtime = _make_runtime()
        runtime.bot = SimpleNamespace(self_id=0)
        runtime.config = SimpleNamespace(master_qq=0, admin_qq=0, self_id=0)
        self.assertEqual(runtime._identity_prompt_block(), '')

    def test_identity_block_master_falls_back_to_admin_qq(self):
        runtime = _make_runtime(master_qq='0', admin_qq='241898129')
        block = runtime._identity_prompt_block()
        self.assertIn('241898129', block)


class ChildBackgroundIdentityLineTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.bot = SimpleNamespace(self_id='1059681169')
        runtime.config = SimpleNamespace(master_qq='241898129', admin_qq='241898129', self_id='0')
        runtime._now_text = lambda: '2026-08-02 12:00'
        runtime._message_source_label = lambda _m: 'QQ 好友'
        runtime._is_master_message = lambda _m: False
        runtime._is_admin_message = lambda _m: False
        runtime._collect_recent_think_notes = lambda _h: []
        runtime._default_knowledge_lines = lambda: []
        runtime._build_mounted_knowledge_prompt_lines = lambda _t, _i: ''
        runtime._normalize_think_note = lambda t: t or ''
        runtime._extract_file_refs = lambda _r: []
        runtime.repo = SimpleNamespace()
        return runtime

    def test_child_background_injects_own_qq_line(self):
        runtime = self._runtime()
        message = SimpleNamespace(
            chat_type='private',
            chat_id='241898129',
            user_id='241898129',
            nickname='主人',
            raw_message='hi',
        )
        result = runtime._build_child_background_prompt(
            message,
            impression='暂无',
            history=[],
            tool_logs=None,
            image_context=None,
            global_identity_context='',
        )
        self.assertIn('你的QQ号（机器人自身账号）: 1059681169', result)
        self.assertIn('不要把自己的账号当成号主账号', result)


if __name__ == '__main__':
    unittest.main()
