import json
import unittest
from unittest.mock import patch

from pack.anthropic_chat_model import AnthropicChatModel


class FakeResponse:
    def __init__(self, events=None, status_code=200, text=''):
        self.events = events or []
        self.status_code = status_code
        self.text = text
        self.encoding = None
        self.closed = False

    def close(self):
        self.closed = True

    def iter_lines(self, decode_unicode=False):
        for event in self.events:
            yield f"data: {json.dumps(event)}".encode('utf-8')
        yield b'data: [DONE]'


class FakeEncoding:
    def encode(self, text):
        return list(text)


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        self.model = AnthropicChatModel('https://example.invalid/v1/messages', '', 'test-model')

    def test_extract_native_usage_shapes(self):
        self.assertEqual(self.model._extract_native_usage({
            'usage': {'input_tokens': 12, 'output_tokens': 7},
        }), (12, 7))
        self.assertEqual(self.model._extract_native_usage({
            'usage': {'prompt_tokens': 20, 'completion_tokens': 9},
        }), (20, 9))

    def test_anthropic_stream_collects_usage(self):
        data = self.model._parse_anthropic_stream(FakeResponse([
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 31}}},
            {'type': 'content_block_start', 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'ok'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 4}},
            {'type': 'message_stop'},
        ]))
        self.assertEqual(data['usage'], {'input_tokens': 31, 'output_tokens': 4})

    def test_openai_stream_collects_usage_chunk_without_choices(self):
        data = self.model._parse_openai_stream(FakeResponse([
            {'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'prompt_tokens': 22, 'completion_tokens': 3}},
        ]))
        self.assertEqual(data['usage']['prompt_tokens'], 22)
        self.assertEqual(data['usage']['completion_tokens'], 3)

    def test_estimate_includes_system_messages_and_tools_with_model_fallback(self):
        payload = {
            'system': 'fixed prompt',
            'messages': [{'role': 'user', 'content': 'hello'}],
            'tools': [{'name': 'lookup', 'description': 'tool definition'}],
        }
        fake_tiktoken = unittest.mock.Mock()
        fake_tiktoken.encoding_for_model.side_effect = KeyError('unknown model')
        fake_tiktoken.get_encoding.return_value = FakeEncoding()
        with patch('pack.anthropic_chat_model.tiktoken', fake_tiktoken):
            input_tokens, output_tokens = self.model._estimate_usage(
                payload, {'content': [{'type': 'text', 'text': 'answer'}]}, 'unknown-model'
            )
        self.assertGreater(input_tokens, len('fixed prompt'))
        self.assertGreater(output_tokens, 0)
        fake_tiktoken.get_encoding.assert_called_once_with('cl100k_base')

    def test_openai_retries_once_without_unsupported_stream_options(self):
        model = AnthropicChatModel(
            'https://example.invalid', '', 'test-model', '/v1/chat/completions'
        )
        rejected = FakeResponse(
            status_code=400,
            text='{"error":{"message":"unknown parameter: stream_options"}}',
        )
        accepted = FakeResponse([
            {'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]},
        ])
        with patch('pack.anthropic_chat_model.requests.post', side_effect=[rejected, accepted]) as post:
            reply = model.complete([], [{'role': 'user', 'content': 'hello'}])

        self.assertEqual(reply.text, 'ok')
        self.assertEqual(post.call_count, 2)
        self.assertIn('stream_options', post.call_args_list[0].kwargs['json'])
        self.assertNotIn('stream_options', post.call_args_list[1].kwargs['json'])
        self.assertTrue(rejected.closed)

    def test_openai_does_not_retry_unrelated_400(self):
        model = AnthropicChatModel(
            'https://example.invalid', '', 'test-model', '/v1/chat/completions'
        )
        rejected = FakeResponse(status_code=400, text='{"error":{"message":"invalid model"}}')
        with patch('pack.anthropic_chat_model.requests.post', return_value=rejected) as post:
            with self.assertRaisesRegex(RuntimeError, 'status=400'):
                model.complete([], [{'role': 'user', 'content': 'hello'}])
        self.assertEqual(post.call_count, 1)


if __name__ == '__main__':
    unittest.main()
