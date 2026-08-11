import json
import unittest

from core.ai_runtime import AIOrchestrator
from core.ai_tools_schema import build_tools


PERSONA = '洛天成，17岁高三女生，说话不用句号'


class ChatModeSystemBlockTests(unittest.TestCase):
    def _runtime(self):
        return object.__new__(AIOrchestrator)

    def _blocks(self, chat_mode: bool, persona=PERSONA, position='last'):
        return self._runtime()._static_system_blocks(
            '工作指令正文', persona, persona_position=position, chat_mode=chat_mode
        )

    # ── 人设块位置 ────────────────────────────────────────────────
    def test_persona_is_a_separate_trailing_block(self):
        blocks = self._blocks(chat_mode=True)
        self.assertEqual(2, len(blocks))
        # 人设必须独立成块，调用方才能把动态背景插到它前面
        self.assertIn(PERSONA, blocks[-1]['text'])
        self.assertNotIn(PERSONA, blocks[0]['text'])

    def test_rules_stay_in_cacheable_first_block(self):
        blocks = self._blocks(chat_mode=True)
        self.assertIn('规则提醒', blocks[0]['text'])
        self.assertEqual({'type': 'ephemeral'}, blocks[0].get('cache_control'))

    def test_inline_position_keeps_single_block(self):
        blocks = self._blocks(chat_mode=True, position='inline')
        self.assertEqual(1, len(blocks))
        self.assertIn(PERSONA, blocks[0]['text'])

    def test_no_persona_means_no_tail_block(self):
        blocks = self._blocks(chat_mode=True, persona=None)
        self.assertEqual(1, len(blocks))

    def test_send_message_contract_travels_with_persona(self):
        # 发言方式和人设同块，别让它留在背景之前
        self.assertIn('send_message', self._blocks(chat_mode=True)[-1]['text'])

    # ── chat / code 内容差异 ──────────────────────────────────────
    def test_chat_mode_injects_focus_and_style(self):
        text = self._blocks(chat_mode=True)[-1]['text']
        self.assertIn('聊天模式', text)
        self.assertIn('notify_master', text)
        self.assertIn('已挂载知识库', text)
        self.assertIn('发送前自检', text)

    def test_code_mode_has_no_chat_only_blocks(self):
        text = '\n'.join(block['text'] for block in self._blocks(chat_mode=False))
        self.assertNotIn('发送前自检', text)
        self.assertNotIn('聊天模式，你的职责', text)

    def test_chat_mode_drops_rules_for_absent_tools(self):
        text = '\n'.join(block['text'] for block in self._blocks(chat_mode=True))
        # 这些规则指向 chat 模式已删掉的工具，留着只是白烧 token
        self.assertNotIn('memory_list', text)
        self.assertNotIn('web_search', text)
        self.assertIn('notify_master', text)

    def test_chat_focus_forbids_guessing_group_slang(self):
        text = self._blocks(chat_mode=True)[-1]['text']
        self.assertIn('黑话', text)
        self.assertIn('stay_silent', text)

    def test_intel_report_survives_silence(self):
        # 沉默轮也要先上报，否则群聊线索全丢
        text = self._blocks(chat_mode=True)[-1]['text']
        self.assertIn('先调用 notify_master，再调用 stay_silent', text)


class ChatModeToolTests(unittest.TestCase):
    def _names(self, chat_mode: bool, **kwargs):
        return {tool['name'] for tool in build_tools(chat_mode=chat_mode, **kwargs)}

    def test_chat_mode_drops_work_tools(self):
        names = self._names(True)
        for name in ('create_agent', 'create_task', 'web_search', 'memory_list',
                     'remember', 'query_logs', 'create_recurring_task', 'download_file'):
            self.assertNotIn(name, names)

    def test_code_mode_keeps_work_tools(self):
        names = self._names(False)
        for name in (
            'create_agent', 'create_task', 'web_search', 'query_logs',
            'find_in_project', 'list_local_files', 'read_local_file',
        ):
            self.assertIn(name, names)

    def test_chat_mode_drops_read_only_code_tools(self):
        names = self._names(True)
        for name in ('find_in_project', 'list_local_files', 'read_local_file'):
            self.assertNotIn(name, names)

    def test_chat_mode_keeps_chat_essentials(self):
        names = self._names(True, include_relation_read=True, include_knowledge_request=True)
        for name in ('send_message', 'notify_master', 'relation_lookup',
                     'request_knowledge_base_update', 'set_session_mode'):
            self.assertIn(name, names)

    def test_chat_mode_keeps_immediate_reply_tools(self):
        names = self._names(True, immediate_mode=True)
        self.assertIn('stay_silent', names)
        self.assertIn('send_sticker', names)

    def test_admin_config_tools_survive_chat_mode(self):
        # 号主私聊默认 chat 模式，配置工具不能因此消失
        self.assertIn('manage_channel', self._names(True, allow_config_tools=True))

    def test_chat_mode_schema_is_smaller(self):
        chat = json.dumps(build_tools(chat_mode=True), ensure_ascii=False)
        code = json.dumps(build_tools(chat_mode=False), ensure_ascii=False)
        self.assertLess(len(chat), len(code) * 0.6)


if __name__ == '__main__':
    unittest.main()
