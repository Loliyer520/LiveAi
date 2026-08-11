import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.ai_runtime import AIOrchestrator
from core.events import ChatMessage


def private_message(text='hello', chat_id=7, user_id=42):
    return ChatMessage(
        chat_type='private',
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        raw_message=text,
        sender={'nickname': 'tester'},
        message_id='m1',
        timestamp=123.0,
        raw_data={},
    )


class AgentIntoModeTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        runtime = object.__new__(AIOrchestrator)
        runtime._scope_direct_agents = {}
        runtime._send_chat_reply = Mock()
        runtime._submit_message = Mock()
        runtime._send_scope_message = Mock(return_value=[
            {'text': 'direct hi', 'raw_message': 'direct hi', 'message_id': 'out-1'}
        ])
        runtime._append_outbound_message_now = Mock()
        runtime._build_outbound_message_entry = Mock(
            side_effect=lambda text, **kwargs: {'text': text, **kwargs}
        )
        runtime._agent_report_delivery = SimpleNamespace(
            build_message=lambda scope_type, scope_id, items: {
                'scope': f'{scope_type}:{scope_id}',
                'items': list(items or []),
            } if items else None
        )
        runtime.agent_manager = SimpleNamespace(
            list_agents=lambda: [],
            get_agent=lambda _agent_id: None,
            send_to_agent=lambda *_args, **_kwargs: True,
        )
        runtime._ensure_agent_loop_running = Mock(return_value={'ok': True, 'started': False, 'error': None})
        return runtime

    async def test_handle_into_command_enters_single_scope_agent(self):
        runtime = self.runtime()
        record = {
            'agent_id': 'agent-1',
            'origin_scope': 'private:7',
            'status': 'waiting',
            'cwd': '/',
            'instruction_summary': 'do work',
        }
        runtime.agent_manager = SimpleNamespace(
            list_agents=lambda: [dict(record)],
            get_agent=lambda agent_id: dict(record) if agent_id == 'agent-1' else None,
            send_to_agent=lambda *_args, **_kwargs: True,
        )

        await runtime._handle_into_command(private_message('#into'), '#into')

        self.assertEqual(runtime._scope_direct_agents, {'private:7': 'agent-1'})
        runtime._send_chat_reply.assert_called_once()
        self.assertIn('已进入 agent 直连模式：agent-1', runtime._send_chat_reply.call_args.args[1])

    async def test_route_into_agent_message_forwards_plain_user_message(self):
        runtime = self.runtime()
        sent = []
        runtime._scope_direct_agents = {'private:7': 'agent-1'}
        runtime.agent_manager = SimpleNamespace(
            list_agents=lambda: [],
            get_agent=lambda _agent_id: {'agent_id': 'agent-1'},
            send_to_agent=lambda agent_id, payload, **_kwargs: sent.append((agent_id, payload)) or True,
        )

        handled = await runtime._route_into_agent_message(private_message('你好'), '你好')

        self.assertTrue(handled)
        self.assertEqual(sent, [('agent-1', {'role': 'user', 'content': '你好'})])
        runtime._send_chat_reply.assert_not_called()

    async def test_deliver_agent_reports_splits_direct_and_relay_items(self):
        runtime = self.runtime()
        runtime._scope_direct_agents = {'private:7': 'agent-direct'}

        runtime._deliver_agent_reports_to_scope(
            'private',
            '7',
            [
                {
                    'agent_id': 'agent-direct',
                    'text': 'direct hi',
                    'ts': 10.0,
                    'origin_scope': 'private:7',
                    'report_type': 'progress',
                },
                {'agent_id': 'agent-other', 'text': 'relay hi', 'ts': 11.0, 'origin_scope': 'private:7'},
                {
                    'agent_id': 'agent-other',
                    'text': 'hidden progress',
                    'ts': 12.0,
                    'origin_scope': 'private:7',
                    'report_type': 'progress',
                },
            ],
        )

        runtime._send_scope_message.assert_called_once()
        self.assertEqual(runtime._send_scope_message.call_args.args[1], 'direct hi')
        runtime._append_outbound_message_now.assert_called_once()
        runtime._submit_message.assert_called_once_with(
            {
                'scope': 'private:7',
                'items': [
                    {'agent_id': 'agent-other', 'text': 'relay hi', 'ts': 11.0, 'origin_scope': 'private:7'}
                ],
            }
        )


if __name__ == '__main__':
    unittest.main()
