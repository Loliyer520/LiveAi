import json
import unittest

from pack.anthropic_chat_model import AnthropicChatModel


class _FakeResponse:
    def __init__(self, lines):
        self._lines = list(lines)

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line


def _lines(*events):
    out = []
    for ev in events:
        out.append(b'data: ' + json.dumps(ev, ensure_ascii=False).encode('utf-8'))
    return out


class ResponsesStreamParsingTests(unittest.TestCase):
    def setUp(self):
        self.model = AnthropicChatModel('https://example.invalid', messages_path='/v1/responses')

    def test_function_call_arguments_from_added_event(self):
        # 部分上游在 added 事件一次性给全参数、不发 delta，参数不能丢
        resp = _FakeResponse(_lines(
            {'type': 'response.created', 'response': {'id': 'r1'}},
            {'type': 'response.output_item.added', 'item': {
                'type': 'function_call', 'call_id': 'fc_1', 'name': 'search',
                'arguments': '{"q": "天气"}'}},
            {'type': 'response.output_item.done', 'item': {'type': 'function_call'}},
            {'type': 'response.completed', 'response': {
                'id': 'r1', 'status': 'completed',
                'usage': {'input_tokens': 5, 'output_tokens': 3}}},
        ))
        data = self.model._parse_openai_responses_stream(resp)
        fc = data['output'][0]
        self.assertEqual(fc['type'], 'function_call')
        self.assertEqual(fc['call_id'], 'fc_1')
        self.assertEqual(fc['name'], 'search')
        self.assertEqual(fc['arguments'], '{"q": "天气"}')
        self.assertEqual(data['status'], 'completed')
        self.assertEqual(data['usage']['input_tokens'], 5)

    def test_message_content_from_added_event(self):
        # added 事件一次性携带 content 而非走 delta 时文本不丢失
        resp = _FakeResponse(_lines(
            {'type': 'response.output_item.added', 'item': {
                'type': 'message',
                'content': [{'type': 'output_text', 'text': '你好'}]}},
            {'type': 'response.output_item.done', 'item': {'type': 'message'}},
            {'type': 'response.completed', 'response': {'id': 'r1', 'status': 'completed'}},
        ))
        data = self.model._parse_openai_responses_stream(resp)
        msg = data['output'][0]
        self.assertEqual(msg['type'], 'message')
        self.assertEqual(msg['content'][0]['text'], '你好')

    def test_mixed_delta_stream(self):
        resp = _FakeResponse(_lines(
            {'type': 'response.output_item.added', 'item': {'type': 'message'}},
            {'type': 'response.output_text.delta', 'delta': '好'},
            {'type': 'response.output_text.delta', 'delta': '的'},
            {'type': 'response.output_item.done', 'item': {'type': 'message'}},
            {'type': 'response.output_item.added', 'item': {
                'type': 'function_call', 'call_id': 'fc_1', 'name': 't', 'arguments': ''}},
            {'type': 'response.function_call_arguments.delta', 'delta': '{"a":1}'},
            {'type': 'response.output_item.done', 'item': {'type': 'function_call'}},
            {'type': 'response.completed', 'response': {'id': 'r1', 'status': 'completed'}},
        ))
        data = self.model._parse_openai_responses_stream(resp)
        self.assertEqual(data['output'][0]['content'][0]['text'], '好的')
        self.assertEqual(data['output'][1]['type'], 'function_call')
        self.assertEqual(data['output'][1]['arguments'], '{"a":1}')

    def test_final_snapshot_without_delta_events(self):
        resp = _FakeResponse(_lines(
            {'type': 'response.created', 'response': {'id': 'r1'}},
            {'type': 'response.completed', 'response': {
                'id': 'r1', 'status': 'completed',
                'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': '最终快照'},
                ]}],
                'usage': {'input_tokens': 2, 'output_tokens': 2},
            }},
        ))
        data = self.model._parse_openai_responses_stream(resp)
        self.assertEqual(data['output'][0]['content'][0]['text'], '最终快照')
        self.assertEqual(data['status'], 'completed')

    def test_output_text_delta_ignored_when_not_message(self):
        resp = _FakeResponse(_lines(
            {'type': 'response.output_item.added', 'item': {
                'type': 'function_call', 'call_id': 'fc_1', 'name': 't', 'arguments': ''}},
            {'type': 'response.output_text.delta', 'delta': '不应被累积'},
            {'type': 'response.function_call_arguments.delta', 'delta': '{}'},
            {'type': 'response.output_item.done', 'item': {'type': 'function_call'}},
            {'type': 'response.completed', 'response': {'id': 'r1', 'status': 'completed'}},
        ))
        data = self.model._parse_openai_responses_stream(resp)
        self.assertEqual(len(data['output']), 1)
        self.assertEqual(data['output'][0]['type'], 'function_call')
        self.assertEqual(data['output'][0]['arguments'], '{}')


class ResponsesNonStreamParsingTests(unittest.TestCase):
    def setUp(self):
        self.model = AnthropicChatModel('https://example.invalid', messages_path='/v1/responses')

    def test_non_stream_response_parsing(self):
        data = {
            'id': 'r1',
            'status': 'completed',
            'output': [
                {'type': 'message', 'content': [
                    {'type': 'output_text', 'text': '你好，有什么可以帮你？'},
                ]},
                {'type': 'function_call', 'call_id': 'fc_1', 'name': 'search',
                 'arguments': '{"q": "test"}'},
            ],
            'usage': {'input_tokens': 10, 'output_tokens': 5},
        }
        blocks, stop_reason = self.model._parse_openai_responses_response(data)
        self.assertEqual(stop_reason, 'end_turn')
        self.assertEqual([b['type'] for b in blocks], ['text', 'tool_use'])
        self.assertEqual(blocks[0]['text'], '你好，有什么可以帮你？')
        self.assertEqual(blocks[1]['name'], 'search')
        self.assertEqual(blocks[1]['input'], {'q': 'test'})

    def test_incomplete_reason_mapping(self):
        blocks, reason = self.model._parse_openai_responses_response({
            'status': 'incomplete',
            'incomplete_details': {'reason': 'max_output_tokens'},
            'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': '截断内容'},
            ]}],
        })
        self.assertEqual(reason, 'max_tokens')
        self.assertEqual(blocks[0]['text'], '截断内容')

        _blocks, reason = self.model._parse_openai_responses_response({
            'status': 'incomplete',
            'incomplete_details': {'reason': 'content_filter'},
            'output': [],
        })
        self.assertEqual(reason, 'content_filter')

    def test_protocol_detection(self):
        self.assertTrue(self.model.is_responses_api)
        self.assertTrue(self.model.is_openai_protocol)


if __name__ == '__main__':
    unittest.main()
