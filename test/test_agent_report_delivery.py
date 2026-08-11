import unittest

from core.agent_report_delivery import AgentReportDeliveryService
from core.event_adapters import envelope_from_scope_turn_item
from core.event_envelope import EventType


class Manager:
    def __init__(self, reports):
        self.reports = list(reports)
        self.requeued = []

    def has_pending_reports(self):
        return bool(self.reports)

    def drain_pending_reports(self):
        reports, self.reports = self.reports, []
        return reports

    def requeue_pending_reports(self, reports):
        self.requeued.extend(reports)


class AgentReportDeliveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = AgentReportDeliveryService()

    def test_flush_delivers_urgent_and_idle_groups_and_requeues_busy_normal(self):
        manager = Manager([
            {'agent_id': 'normal', 'text': 'later', 'origin_scope': 'group:7'},
            {'agent_id': 'urgent', 'text': 'now', 'origin_scope': 'group:7', 'urgent': True},
            {'agent_id': 'idle', 'text': 'deliver', 'origin_scope': 'group:8'},
        ])
        delivered = []

        self.service.flush(
            manager,
            is_scope_active=lambda scope_key: scope_key == 'group:7',
            deliver=lambda scope_type, scope_id, items: delivered.append(
                (f'{scope_type}:{scope_id}', [item['agent_id'] for item in items])
            ),
        )

        self.assertEqual(delivered, [('group:7', ['urgent']), ('group:8', ['idle'])])
        self.assertEqual([item['agent_id'] for item in manager.requeued], ['normal'])

    def test_flush_without_idle_gate_delivers_normal_busy_group(self):
        manager = Manager([
            {'agent_id': 'normal', 'text': 'now', 'origin_scope': 'group:7'},
        ])
        delivered = []

        self.service.flush(
            manager,
            is_scope_active=lambda _scope_key: True,
            deliver=lambda scope_type, scope_id, items: delivered.append(
                (scope_type, scope_id, items)
            ),
            only_if_idle=False,
        )

        self.assertEqual(delivered[0][0:2], ('group', '7'))
        self.assertEqual(manager.requeued, [])

    def test_invalid_scope_falls_back_and_message_keeps_agent_metadata(self):
        self.assertEqual(self.service.parse_scope(None), ('master', '0'))
        self.assertEqual(self.service.parse_scope('group:'), ('master', '0'))

        message = self.service.build_message('group', 'not-an-int', [
            {'agent_id': 'a1', 'text': 'first'},
            {'agent_id': 'a2', 'text': 'second'},
        ])

        self.assertEqual(message.chat_type, 'master')
        self.assertEqual(message.chat_id, 0)
        self.assertIn('【agent#a1】\nfirst', message.text)
        self.assertIn('【agent#a2】\nsecond', message.text)
        self.assertEqual(message.raw_data, {
            'source': 'agent_message',
            'system_event': 'agent_message',
            'agent_count': 2,
        })
        self.assertTrue(message.mentions_self)
        envelope = envelope_from_scope_turn_item({
            'kind': 'message',
            'message': message,
        })
        self.assertEqual(envelope.event_type, EventType.AGENT_REPORT)
        self.assertEqual(envelope.scope_key, 'master:0')


if __name__ == '__main__':
    unittest.main()
