import unittest

from core.dev_agent import RetryableAPIError, _complete_with_valid_response
from pack.anthropic_chat_model import AnthropicChatModel, AnthropicReply


class _StubModel:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def complete(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


class ModelResponseInitializationTests(unittest.TestCase):
    def call(self, model, *, require_content=False):
        return _complete_with_valid_response(
            model, 'system', [{'role': 'user', 'content': 'hello'}], [], 0.4, 4096,
            require_content=require_content,
        )

    def test_normal_response_is_returned(self):
        expected = AnthropicReply(text='ok')
        self.assertIs(self.call(_StubModel(result=expected), require_content=True), expected)

    def test_api_exception_propagates_without_unbound_response(self):
        with self.assertRaisesRegex(RuntimeError, 'upstream failed'):
            self.call(_StubModel(error=RuntimeError('upstream failed')), require_content=True)

    def test_none_response_raises_retryable_error(self):
        with self.assertRaisesRegex(RetryableAPIError, '没有返回有效响应'):
            self.call(_StubModel(result=None))

    def test_empty_response_protocol_branch_is_rejected_for_agent(self):
        with self.assertRaisesRegex(RetryableAPIError, '空内容'):
            self.call(_StubModel(result=AnthropicReply()), require_content=True)

    def test_streaming_and_non_streaming_protocol_flags_do_not_change_initialization(self):
        cases = (
            ('/v1/responses', True),
            ('/v1/chat/completions', False),
            ('/v1/messages', False),
        )
        for path, is_responses in cases:
            with self.subTest(path=path):
                protocol_model = AnthropicChatModel('https://example.invalid', messages_path=path)
                self.assertEqual(protocol_model.is_responses_api, is_responses)
                for stream in (True, False):
                    reply = AnthropicReply(text=f'{path}:{stream}')
                    stub = _StubModel(result=reply)
                    self.assertIs(self.call(stub, require_content=True), reply)
                    self.assertEqual(len(stub.calls), 1)


if __name__ == '__main__':
    unittest.main()
