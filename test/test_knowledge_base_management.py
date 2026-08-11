import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_repository import AIRepository
from core.ai_runtime import AIOrchestrator
from core.ai_tools_schema import build_tools
from pack.json_store import JsonStore


class KnowledgeBaseRepositoryTests(unittest.TestCase):
    def test_multi_base_mount_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = AIRepository(JsonStore(str(Path(tmp) / 'state.json')))

            default_info = repo.get_knowledge_base_info(repo.DEFAULT_KNOWLEDGE_BASE_ID)
            self.assertIsNotNone(default_info)
            self.assertEqual(default_info['name'], repo.DEFAULT_KNOWLEDGE_BASE_NAME)

            kb = repo.create_knowledge_base('群聊设定', '给某个群分身使用的额外事实')
            self.assertIsNotNone(kb)
            entry = repo.add_knowledge_entry('这个群偏好简短回答。', kb['kb_id'])
            self.assertIsNotNone(entry)

            mounts = repo.set_scope_knowledge_mounts('group', '123', [kb['kb_id'], 'missing'])
            self.assertEqual(mounts, [kb['kb_id']])
            self.assertEqual(repo.get_scope_knowledge_mounts('group', '123'), [kb['kb_id']])

            self.assertTrue(repo.delete_knowledge_base_info(kb['kb_id']))
            self.assertEqual(repo.get_scope_knowledge_mounts('group', '123'), [])


class KnowledgeBaseToolTests(unittest.IsolatedAsyncioTestCase):
    def test_build_tools_exposes_knowledge_tools(self):
        names = {item['name'] for item in build_tools(include_knowledge_management=True, include_knowledge_request=True)}
        self.assertIn('manage_knowledge_base', names)
        self.assertIn('request_knowledge_base_update', names)

    async def test_request_knowledge_base_update_creates_notify_master_task(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.tools = SimpleNamespace(
            create_task=Mock(return_value=SimpleNamespace(task_id='kb-task-1')),
            record_tool_use=Mock(),
        )
        runtime.config = SimpleNamespace(history_limit=20, admin_qq='1')

        result = await AIOrchestrator._run_ai_tool_call(
            runtime,
            'group',
            '321',
            'agent:group:321',
            'request_knowledge_base_update',
            {'suggestion': '建议把“这个群只接受简洁通知”补进群知识库'},
        )

        runtime.tools.create_task.assert_called_once()
        args = runtime.tools.create_task.call_args.args
        self.assertEqual(args[1], 'notify_master')
        self.assertEqual(args[2]['request_type'], 'knowledge_base_suggestion')
        self.assertIn('建议把', args[2]['suggestion'])
        self.assertIn('kb-task-1', result)

    async def test_knowledge_base_suggestion_without_text_still_gets_followup(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.repo = SimpleNamespace(
            get_or_create_master=Mock(),
            add_note=Mock(),
        )
        runtime.tools = SimpleNamespace(
            create_task=Mock(return_value=SimpleNamespace(task_id='followup-1')),
        )
        runtime._build_master_prompt = lambda _task: 'prompt'
        runtime._static_system_blocks = lambda _prompt: []
        runtime._master_system_prompt = lambda: ''
        runtime._scope_key = AIOrchestrator._scope_key.__get__(runtime, AIOrchestrator)
        runtime._should_callback_to_source = lambda _payload, _text: True
        runtime._find_task_by_trace = lambda *_args, **_kwargs: None
        runtime._build_followup_instruction = lambda payload, text: f"FOLLOW:{text}"
        runtime._submit_runtime_task = Mock()

        async def fake_complete_chat(*_args, **_kwargs):
            return SimpleNamespace(text='', raw_content='', tool_calls=[])

        runtime._complete_chat = fake_complete_chat

        result = await AIOrchestrator._handle_notify_master(
            runtime,
            {
                'task_id': 'notify-1',
                'source_agent': 'child:group:321',
                'kind': 'notify_master',
                'payload': {
                    'scope_type': 'group',
                    'scope_id': '321',
                    'request_type': 'knowledge_base_suggestion',
                    'suggestion': '建议新增一条知识',
                    'trace_id': 'trace-kb-1',
                },
            },
        )

        runtime.tools.create_task.assert_called_once()
        payload = runtime.tools.create_task.call_args.args[2]
        self.assertEqual(runtime.tools.create_task.call_args.args[1], 'followup_to_child')
        self.assertIn('批准或拒绝', payload['instruction'])
        self.assertIn('followup-1', result)


if __name__ == '__main__':
    unittest.main()
