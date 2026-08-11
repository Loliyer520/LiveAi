import copy
import unittest

from core.event_adapters import (
    create_scoped_event,
    envelope_from_agent_report,
    envelope_from_normalized_event,
    envelope_from_scope_turn_item,
    scope_turn_item_from_batch,
    scope_turn_item_from_envelope,
)
from core.event_envelope import EventEnvelope, EventType
from core.event_mailbox import InMemoryEventMailbox
from core.event_normalizer import normalize_ws_event
from core.events import ChatMessage


class EventAdapterContractTests(unittest.TestCase):
    def message_item(self, text='hello', message_id=1, timestamp=100.0, **extra):
        item = {
            'kind': 'message',
            'message': ChatMessage(
                'group', 7, 9, text, raw_message=text,
                sender={'nickname': 'u', 'tags': ['original']},
                message_id=message_id, timestamp=timestamp,
                raw_data={'source': 'test', 'nested': {'value': 1}},
            ),
            'cleaned': text,
            'agent_id': 'agent-1',
            'deferred_count': 1,
            'trigger_messages': [{'text': text, 'metadata': {'index': 1}}],
            'message_epoch': 3,
            'history_seed': [{'role': 'assistant', 'content': 'seed'}],
            'metadata': {'trace': ['message']},
        }
        item.update(extra)
        return item

    def test_normalized_message_adapter_preserves_existing_scope_and_identity(self):
        normalized = normalize_ws_event({
            'post_type': 'message',
            'message_type': 'group',
            'group_id': 7,
            'user_id': 9,
            'message_id': 11,
            'raw_message': 'hello',
            'message': [{'type': 'text', 'data': {'text': 'hello'}}],
            'time': 123,
        }, self_id=241898129)
        envelope = envelope_from_normalized_event(normalized)
        self.assertEqual(envelope.event_type, EventType.MESSAGE)
        self.assertEqual(envelope.scope_key, 'group:7')
        self.assertEqual(envelope.event_id, normalized.source_key)
        self.assertEqual(envelope.payload['message_id'], 11)

    def test_agent_report_origin_scope_ts_and_extra_metadata_round_trip(self):
        report = {
            'agent_id': 'agent-1',
            'text': 'done',
            'ts': '125.50',
            'origin_scope': 'private:9',
            'urgent': True,
            'metadata': {'attempts': [1, 2]},
        }
        envelope = envelope_from_agent_report(report)
        restored = EventEnvelope.from_dict(envelope.to_dict())
        self.assertEqual(restored.event_type, EventType.AGENT_REPORT)
        self.assertEqual(restored.scope_key, 'private:9')
        self.assertEqual(restored.occurred_at, 125.5)
        self.assertEqual(restored.payload, report)

    def test_generic_scoped_adapter_does_not_select_delivery_policy(self):
        payload = {'text': 'wake', 'metadata': {'labels': ['a']}}
        envelope = create_scoped_event(
            EventType.ALARM, 'group:7', payload,
            source='alarm-manager', event_id='alarm-1', occurred_at=126.0,
        )
        self.assertEqual(envelope.event_type, EventType.ALARM)
        self.assertEqual(envelope.scope_key, 'group:7')
        self.assertIsNone(envelope.mailbox_sequence)
        payload['metadata']['labels'].append('mutated')
        self.assertEqual(envelope.payload['metadata']['labels'], ['a'])

    def test_message_round_trip_preserves_trigger_metadata_history_and_scalar_values(self):
        for message_id, timestamp in ((None, None), ('', ''), ('0012', '123.50')):
            with self.subTest(message_id=message_id, timestamp=timestamp):
                item = self.message_item(
                    message_id=message_id,
                    timestamp=timestamp,
                    silent_event=True,
                )
                restored = scope_turn_item_from_envelope(envelope_from_scope_turn_item(item))
                self.assertEqual(restored['message'].message_id, message_id)
                self.assertEqual(restored['message'].timestamp, timestamp)
                self.assertEqual(restored['trigger_messages'], item['trigger_messages'])
                self.assertEqual(restored['metadata'], item['metadata'])
                self.assertEqual(restored['history_seed'], item['history_seed'])
                self.assertTrue(restored['silent_event'])

    def test_scope_turn_adapter_classifies_existing_agent_report_shape(self):
        item = self.message_item(
            text='report',
            kind='report',
            message=ChatMessage(
                'private', 9, 241898129, 'report', raw_message='report',
                sender={'nickname': 'system'}, timestamp=102.0,
                raw_data={'system_event': 'agent_message'},
            ),
        )
        envelope = envelope_from_scope_turn_item(item)
        self.assertEqual(envelope.event_type, EventType.AGENT_REPORT)
        self.assertEqual(envelope.scope_key, 'private:9')
        self.assertEqual(scope_turn_item_from_envelope(envelope)['kind'], 'report')

    def test_mixed_message_task_report_batch_is_fifo_and_lossless(self):
        message = self.message_item(metadata={'position': 1})
        task = {
            'kind': 'task', 'scope_key': 'group:7', 'task_id': 'task-1',
            'message_epoch': 4, 'trigger_messages': [{'text': 'task'}],
            'metadata': {'position': 2}, 'history_seed': [{'role': 'user', 'content': 'task-seed'}],
        }
        report = {
            'kind': 'report', 'scope_key': 'group:7', 'report_id': 'report-1',
            'text': 'done', 'trigger_messages': [{'text': 'report'}],
            'metadata': {'position': 3},
        }
        mailbox = InMemoryEventMailbox()
        mailbox.append_many(envelope_from_scope_turn_item(item) for item in (message, task, report))
        merged = scope_turn_item_from_batch(mailbox.drain_scope('group:7'))
        self.assertEqual([item['kind'] for item in merged['batch_items']], ['message', 'task', 'report'])
        self.assertEqual([item['metadata']['position'] for item in merged['batch_items']], [1, 2, 3])
        self.assertEqual([entry['text'] for entry in merged['trigger_messages']], ['hello', 'task', 'report'])
        self.assertEqual(merged['history_seed'], message['history_seed'])
        self.assertEqual(merged['mailbox_sequences'], sorted(merged['mailbox_sequences']))

    def test_missing_fields_unknown_kind_and_non_mapping_payload_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, 'include kind'):
            envelope_from_scope_turn_item({'message': self.message_item()['message']})
        with self.assertRaisesRegex(ValueError, 'unsupported scope turn item kind'):
            envelope_from_scope_turn_item({'kind': 'mystery', 'scope_key': 'group:7'})
        with self.assertRaisesRegex(ValueError, 'must include message'):
            envelope_from_scope_turn_item({'kind': 'message'})
        with self.assertRaisesRegex(ValueError, 'must include scope_key'):
            envelope_from_scope_turn_item({'kind': 'task', 'task_id': 't'})
        with self.assertRaisesRegex(TypeError, 'payload must be a mapping'):
            create_scoped_event(EventType.ALARM, 'group:7', ['bad'], source='x', event_id='x', occurred_at=1)
        with self.assertRaisesRegex(TypeError, 'event payload item must be a mapping'):
            scope_turn_item_from_envelope(EventEnvelope(
                EventType.RECURRING_TASK, 'group', '7', {'kind': 'task', 'item': ['bad']},
                source='task', event_id='t', occurred_at=1,
            ))

    def test_agent_report_required_fields_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, 'include origin_scope'):
            envelope_from_agent_report({'agent_id': 'a', 'ts': 1})
        with self.assertRaisesRegex(ValueError, 'include agent_id'):
            envelope_from_agent_report({'origin_scope': 'group:7', 'ts': 1})
        with self.assertRaisesRegex(ValueError, 'include ts'):
            envelope_from_agent_report({'origin_scope': 'group:7', 'agent_id': 'a'})
        with self.assertRaisesRegex(TypeError, 'report must be a mapping'):
            envelope_from_agent_report(['bad'])

    def test_input_dict_is_not_modified_and_nested_aliases_are_broken(self):
        item = self.message_item()
        before = copy.deepcopy(item)
        envelope = envelope_from_scope_turn_item(item)
        self.assertEqual(item, before)
        item['trigger_messages'][0]['metadata']['index'] = 99
        item['history_seed'][0]['content'] = 'mutated'
        item['metadata']['trace'].append('mutated')
        item['message'].sender['tags'].append('mutated')
        item['message'].raw_data['nested']['value'] = 99
        restored = scope_turn_item_from_envelope(envelope)
        self.assertEqual(restored['trigger_messages'][0]['metadata']['index'], 1)
        self.assertEqual(restored['history_seed'][0]['content'], 'seed')
        self.assertEqual(restored['metadata']['trace'], ['message'])
        self.assertEqual(restored['message'].sender['tags'], ['original'])
        self.assertEqual(restored['message'].raw_data['nested']['value'], 1)
        restored['metadata']['trace'].append('restored-mutation')
        self.assertEqual(envelope.payload['metadata']['trace'], ['message'])


if __name__ == '__main__':
    unittest.main()
