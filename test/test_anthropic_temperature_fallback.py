import unittest
from unittest.mock import patch

from pack.anthropic_chat_model import AnthropicChatModel

_DEPRECATED_BODY = (
    '{"type":"error","error":{"type":"invalid_request_error",'
    '"message":"`temperature` is deprecated for this model."}}'
)


class _FakeResponse:
    def __init__(self, status_code, text='{"type":"error"}'):
        self.status_code = status_code
        self.text = text
        self.encoding = 'utf-8'
        self.closed = False

    def close(self):
        self.closed = True

    def iter_lines(self, *_a, **_kw):
        return iter(())


class TemperatureDetectorTests(unittest.TestCase):
    def test_detects_deprecated_message(self):
        self.assertTrue(AnthropicChatModel._temperature_unsupported(_FakeResponse(400, _DEPRECATED_BODY)))

    def test_ignores_non_4xx(self):
        self.assertFalse(AnthropicChatModel._temperature_unsupported(_FakeResponse(200, _DEPRECATED_BODY)))
        self.assertFalse(AnthropicChatModel._temperature_unsupported(_FakeResponse(500, _DEPRECATED_BODY)))

    def test_ignores_unrelated_400(self):
        body = '{"error":{"message":"max_tokens: Input should be a valid integer"}}'
        self.assertFalse(AnthropicChatModel._temperature_unsupported(_FakeResponse(400, body)))

    def test_requires_an_unsupported_marker(self):
        # 只提 temperature 但没说不支持（比如说值超范围），不该走去掉参数的回退
        body = '{"error":{"message":"temperature must be between 0 and 1"}}'
        self.assertFalse(AnthropicChatModel._temperature_unsupported(_FakeResponse(400, body)))

    def test_survives_unreadable_body(self):
        class _Broken:
            status_code = 400

            @property
            def text(self):
                raise ValueError('boom')

        self.assertFalse(AnthropicChatModel._temperature_unsupported(_Broken()))


class TemperatureFallbackTests(unittest.TestCase):
    """该渠道弃用了 temperature，带上就 400；应本地去掉重试一次而不是中断会话。"""

    def _model(self):
        return AnthropicChatModel('https://example.invalid', messages_path='/v1/messages')

    def _run(self, model, bodies, thinking=None, temperature=0.7):
        sent = []
        responses = [_FakeResponse(code, text) for code, text in bodies]

        def _fake_post(_url, headers=None, json=None, **_kw):
            sent.append(json)
            return responses[min(len(sent) - 1, len(responses) - 1)]

        with patch('pack.anthropic_chat_model.requests.post', _fake_post):
            with self.assertRaises(RuntimeError):
                model.complete(
                    'sys',
                    [{'role': 'user', 'content': 'hi'}],
                    None,
                    'claude-opus-5',
                    temperature,
                    None,
                    thinking=thinking,
                )
        return sent

    def test_retries_once_without_temperature(self):
        sent = self._run(self._model(), [(400, _DEPRECATED_BODY), (400, _DEPRECATED_BODY)])

        self.assertEqual(2, len(sent))
        self.assertEqual(0.7, sent[0]['temperature'])
        self.assertNotIn('temperature', sent[1])

    def test_fallback_keeps_the_rest_of_the_payload(self):
        sent = self._run(self._model(), [(400, _DEPRECATED_BODY), (400, _DEPRECATED_BODY)])
        first, second = sent
        self.assertEqual(first['max_tokens'], second['max_tokens'])
        self.assertEqual(first['messages'], second['messages'])
        self.assertEqual(first['system'], second['system'])

    def test_rejection_is_remembered_for_later_calls(self):
        model = self._model()
        self._run(model, [(400, _DEPRECATED_BODY), (400, _DEPRECATED_BODY)])
        self.assertTrue(model._temperature_rejected)

        # 第二次调用应该一开始就不带 temperature，不再白跑一次被拒的请求
        sent = self._run(model, [(400, '{"type":"error"}')])
        self.assertEqual(1, len(sent))
        self.assertNotIn('temperature', sent[0])

    def test_flag_starts_false(self):
        self.assertFalse(self._model()._temperature_rejected)

    def test_no_fallback_for_unrelated_400(self):
        body = '{"error":{"message":"model not found"}}'
        sent = self._run(self._model(), [(400, body)])
        self.assertEqual(1, len(sent))
        self.assertFalse(self._model()._temperature_rejected)

    def test_thinking_fallback_runs_first_then_temperature(self):
        # thinking 回退会把 temperature 重新写回 payload，所以温度回退必须排在它之后
        model = self._model()
        sent = self._run(
            model,
            [(400, _DEPRECATED_BODY), (400, _DEPRECATED_BODY), (400, _DEPRECATED_BODY)],
            thinking='high',
        )

        self.assertEqual(3, len(sent))
        self.assertEqual(1.0, sent[0]['temperature'])  # thinking 强制 1.0
        self.assertIn('thinking', sent[0])
        self.assertEqual(0.7, sent[1]['temperature'])  # 去掉 thinking，还原调用方温度
        self.assertNotIn('thinking', sent[1])
        self.assertNotIn('temperature', sent[2])  # 再去掉 temperature
        self.assertNotIn('thinking', sent[2])

    def test_remembered_rejection_applies_to_thinking_fallback(self):
        model = self._model()
        model._temperature_rejected = True
        sent = self._run(model, [(400, '{"type":"error"}')] * 2, thinking='high')

        self.assertEqual(2, len(sent))
        for payload in sent:
            self.assertNotIn('temperature', payload)


if __name__ == '__main__':
    unittest.main()
