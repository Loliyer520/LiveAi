import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.ai_repository import AIRepository
from core.ai_runtime import AIOrchestrator
from core.ai_tools_schema import build_tools
from core.agent_manager import AgentManager
from core.event_normalizer import NormalizedEvent, normalize_ws_event, scope_for
from core.events import ChatMessage
from core.event_mailbox import InMemoryEventMailbox
from core.character_session import CharacterSessionRegistry
from core.model_manager import ModelManager
from pack.json_store import JsonStore
from pack.webui_server import WebUIService


class Stage0InterfaceCharacterizationTests(unittest.TestCase):
    def test_public_entrypoints_and_signatures_are_importable(self):
        import main  # noqa: F401 - verifies the production entry module is importable

        self.assertTrue(callable(main.build_app))
        self.assertEqual(str(inspect.signature(main.build_app)), '() -> pack.napcat.NapcatBot')
        for cls in (AIOrchestrator, AIRepository, AgentManager, ModelManager, WebUIService):
            self.assertTrue(inspect.isclass(cls))
        self.assertEqual(str(inspect.signature(AIOrchestrator.handle_group_message)),
                         '(self, message: core.events.ChatMessage)')
        self.assertEqual(str(inspect.signature(AIOrchestrator.handle_private_message)),
                         '(self, message: core.events.ChatMessage)')
        self.assertEqual(str(inspect.signature(AIRepository.load_state)), '(self) -> dict')
        self.assertEqual(str(inspect.signature(AgentManager.drain_pending_reports)),
                         '(self) -> list[dict]')

    def test_event_normalization_and_scope_derivation(self):
        self.assertEqual(scope_for('group', 123), ('group:123', 'group', '123'))
        self.assertEqual(scope_for('private', '456'), ('private:456', 'private', '456'))
        self.assertEqual(scope_for('unknown', '1'), (None, None, None))
        event = normalize_ws_event(
            {
                'post_type': 'message',
                'message_type': 'group',
                'group_id': 123,
                'user_id': 9,
                'message_id': '77',
                'raw_message': 'hello',
                'message': [{'type': 'text', 'data': {'text': 'hello'}}],
            },
            241898129,
        )
        self.assertIsInstance(event, NormalizedEvent)
        self.assertEqual(event.scope_key, 'group:123')
        self.assertEqual(event.scope_id, '123')

    def test_scope_reservation_is_serial_and_pending_is_fifo(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._character_sessions = CharacterSessionRegistry()
        runtime._event_mailbox = InMemoryEventMailbox()
        runtime._is_message_stale = lambda message: False
        first = ChatMessage(
            chat_type='group', chat_id=7, user_id=1, text='one',
            raw_message='one', sender={'nickname': 'one'}, message_id=1,
        )
        second = ChatMessage(
            chat_type='group', chat_id=7, user_id=2, text='two',
            raw_message='two', sender={'nickname': 'two'}, message_id=2,
        )
        first_item = {'message': first, 'cleaned': 'one', 'agent_id': 'a'}
        second_item = {'message': second, 'cleaned': 'two', 'agent_id': 'a'}
        self.assertTrue(runtime._reserve_scope_turn(first_item))
        self.assertFalse(runtime._reserve_scope_turn(second_item))
        followup = runtime._take_pending_scope_turn(first_item)
        self.assertIs(followup['message'], second)
        self.assertIsNone(runtime._take_pending_scope_turn(first_item))

    def test_model_fallback_and_legacy_tasker_role(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'models.json'
            path.write_text(json.dumps({
                'upstreams': [
                    {'name': 'u', 'base_url': 'http://example.invalid', 'api_key': 'x', 'messages_path': '/v1/messages'}
                ],
                'channels': [{'name': 'main-channel', 'strategy': 'fallback', 'models': [
                    {'upstream': 'u', 'model_id': 'one'}, {'upstream': 'u', 'model_id': 'two'}
                ]}],
                'roles': {'main': 'main-channel', 'dev_agent': 'main-channel'},
            }), encoding='utf-8')
            manager = ModelManager(str(path))
            self.assertEqual(manager.config['roles']['tasker'], 'main-channel')
            self.assertEqual(manager.get_model_for_role('tasker')['model_name'], 'one')
            manager.notify_failure('main')
            self.assertEqual(manager.get_model_for_role('main')['model_name'], 'two')

    def test_model_fallback_reset_restarts_from_first_each_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'models.json'
            path.write_text(json.dumps({
                'upstreams': [
                    {'name': 'u', 'base_url': 'http://example.invalid', 'api_key': 'x', 'messages_path': '/v1/messages'}
                ],
                'channels': [{'name': 'main-channel', 'strategy': 'fallback_reset', 'models': [
                    {'upstream': 'u', 'model_id': 'one'}, {'upstream': 'u', 'model_id': 'two'}
                ]}],
                'roles': {'main': 'main-channel'},
            }), encoding='utf-8')
            manager = ModelManager(str(path))
            manager.begin_request('main')
            self.assertEqual(manager.get_model_for_role('main')['model_name'], 'one')
            manager.notify_failure('main')
            self.assertEqual(manager.get_model_for_role('main')['model_name'], 'two')
            manager.begin_request('main')
            self.assertEqual(manager.get_model_for_role('main')['model_name'], 'one')

    def test_tool_schema_and_tool_result_shape(self):
        tools = build_tools()
        self.assertGreater(len(tools), 0)
        for tool in tools:
            self.assertTrue({'name', 'description', 'input_schema'} <= set(tool))
            self.assertEqual(tool['input_schema']['type'], 'object')
        tool_call = {'type': 'tool_use', 'id': 'call-1', 'name': 'stay_silent', 'input': {}}
        assistant = {'role': 'assistant', 'content': [tool_call]}
        result = {'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'ok'}
        messages = [assistant, {'role': 'user', 'content': [result]}]
        self.assertEqual(messages[0]['content'][0]['id'], messages[1]['content'][0]['tool_use_id'])
        self.assertEqual(messages[0]['role'], 'assistant')
        self.assertEqual(messages[1]['role'], 'user')

    def test_agent_report_drain_and_requeue_preserves_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AgentManager(storage_path=str(Path(temp_dir) / 'agents.json'))
            agent_id = manager.create_agent('test', origin_scope='group:7')
            manager.on_agent_message(agent_id, 'first')
            manager.on_agent_message(agent_id, 'second')
            reports = manager.drain_pending_reports()
            self.assertEqual([item['text'] for item in reports], ['first', 'second'])
            self.assertFalse(manager.has_pending_reports())
            manager.requeue_pending_reports(reports)
            self.assertEqual([item['text'] for item in manager.peek_pending_reports()], ['first', 'second'])
            self.assertEqual(manager.drain_pending_reports()[0]['origin_scope'], 'group:7')

    def test_old_json_state_is_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'state.json'
            path.write_text('{"agents": {"old": {"status": "idle"}}} trailing-junk', encoding='utf-8')
            state = JsonStore(str(path)).load()
            self.assertEqual(state['agents']['old']['status'], 'idle')

    def test_runtime_status_contract_is_stable(self):
        runtime = object.__new__(AIOrchestrator)
        runtime.config = SimpleNamespace(
            enabled=True,
            worker_count=2,
            chat_model_workers=8,
            background_workers=2,
        )
        runtime.loop = object()
        runtime.queue = SimpleNamespace(qsize=lambda: 3)
        runtime._event_mailbox = SimpleNamespace(pending_count=lambda: 4)
        runtime._scope_dispatcher = SimpleNamespace(active_actor_count=lambda: 2)
        runtime._scheduled_alarm_ids = {'alarm-1'}
        runtime.model_manager = SimpleNamespace(
            config={'roles': {'main': 'main-channel'}, 'channels': [
                {'name': 'main-channel', 'strategy': 'fallback', 'models': [
                    {'upstream': 'u', 'model_id': 'model-1'}
                ]}
            ]},
            get_current_model=lambda: {'display_name': 'Main', 'model_name': 'model-1'},
        )
        status = runtime.get_runtime_status()
        self.assertEqual(
            set(status),
            {
                'enabled', 'ready', 'active_profile', 'active_model', 'active_label',
                'queue_size', 'worker_count', 'active_actor_count', 'task_ingress_size',
                'chat_model_workers', 'background_workers', 'scheduled_alarm_count',
                'available_models',
            },
        )
        self.assertEqual(status['queue_size'], 4)
        self.assertEqual(status['active_actor_count'], 2)
        self.assertEqual(status['task_ingress_size'], 3)
        self.assertEqual(status['scheduled_alarm_count'], 1)
        self.assertEqual(status['available_models'][0]['model_id'], 'u/model-1')


if __name__ == '__main__':
    unittest.main()
