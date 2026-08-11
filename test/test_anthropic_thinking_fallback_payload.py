import unittest
from unittest.mock import patch

from pack.anthropic_chat_model import _ANTHROPIC_DEFAULT_MAX_TOKENS, AnthropicChatModel


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


class ThinkingFallbackPayloadTests(unittest.TestCase):
    """thinking 回退请求曾把 max_tokens 写回 None，上游报 Input should be a valid integer。"""

    def _post_payloads(self, max_tokens=None, thinking='high', statuses=(400, 400)):
        model = AnthropicChatModel('https://example.invalid', messages_path='/v1/messages')
        sent = []
        responses = [_FakeResponse(code) for code in statuses]

        def _fake_post(_url, headers=None, json=None, **_kw):
            sent.append(json)
            return responses[min(len(sent) - 1, len(responses) - 1)]

        with patch('pack.anthropic_chat_model.requests.post', _fake_post):
            with self.assertRaises(RuntimeError):
                model.complete(
                    'sys',
                    [{'role': 'user', 'content': 'hi'}],
                    None,
                    'claude-opus-4-7',
                    0.7,
                    max_tokens,
                    thinking=thinking,
                )
        return sent

    def test_fallback_keeps_integer_max_tokens_when_caller_passed_none(self):
        sent = self._post_payloads(max_tokens=None)

        self.assertEqual(2, len(sent), '开了 thinking 且首请求 400 时应该发一次回退请求')
        fallback = sent[1]
        self.assertIsInstance(fallback['max_tokens'], int)
        self.assertEqual(_ANTHROPIC_DEFAULT_MAX_TOKENS, fallback['max_tokens'])
        # 回退的意义就是去掉 thinking，别把它一起带过去
        self.assertNotIn('thinking', fallback)

    def test_fallback_preserves_explicit_max_tokens(self):
        sent = self._post_payloads(max_tokens=8192)
        self.assertEqual(8192, sent[1]['max_tokens'])

    def test_fallback_restores_caller_temperature(self):
        # thinking 强制 temperature=1.0，去掉 thinking 后要还原调用方的值
        sent = self._post_payloads()
        self.assertEqual(1.0, sent[0]['temperature'])
        self.assertEqual(0.7, sent[1]['temperature'])

    def test_first_request_max_tokens_is_integer(self):
        sent = self._post_payloads(statuses=(400, 400))
        self.assertIsInstance(sent[0]['max_tokens'], int)

    def test_no_fallback_without_thinking(self):
        sent = self._post_payloads(thinking=None)
        self.assertEqual(1, len(sent), '没开 thinking 时 400 不该触发 reasoning 回退')

    def test_no_fallback_when_first_request_succeeds(self):
        model = AnthropicChatModel('https://example.invalid', messages_path='/v1/messages')
        sent = []

        def _fake_post(_url, headers=None, json=None, **_kw):
            sent.append(json)
            return _FakeResponse(200)

        with patch('pack.anthropic_chat_model.requests.post', _fake_post):
            # 空流会另报"返回空内容"，这里只关心没多发一次回退请求
            with self.assertRaises(RuntimeError):
                model.complete('sys', [{'role': 'user', 'content': 'hi'}], None, 'm', 0.7, None, thinking='high')

        self.assertEqual(1, len(sent))


if __name__ == '__main__':
    unittest.main()
