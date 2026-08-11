import unittest

from core.ai_runtime import AIOrchestrator
from pack.anthropic_chat_model import AnthropicChatModel


class PromptCacheControlTests(unittest.TestCase):
    def test_stamp_cache_control_on_string_message(self):
        message = {'role': 'user', 'content': 'hello'}

        AIOrchestrator._stamp_cache_control_on_message(message)

        self.assertEqual(
            message['content'],
            [
                {
                    'type': 'text',
                    'text': 'hello',
                    'cache_control': {'type': 'ephemeral'},
                }
            ],
        )

    def test_static_system_blocks_are_marked_cacheable(self):
        runtime = object.__new__(AIOrchestrator)

        blocks = runtime._static_system_blocks('system prompt')

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['type'], 'text')
        self.assertEqual(blocks[0]['cache_control'], {'type': 'ephemeral'})

    def test_anthropic_normalization_preserves_ephemeral_cache_control_only(self):
        model = AnthropicChatModel('https://example.com', messages_path='/v1/messages')

        normalized = model._normalize_anthropic_message_content(
            [
                {
                    'type': 'text',
                    'text': 'cached',
                    'cache_control': {'type': 'ephemeral'},
                },
                {
                    'type': 'text',
                    'text': 'ignored',
                    'cache_control': {'type': 'other'},
                },
            ]
        )

        self.assertEqual(
            normalized,
            [
                {
                    'type': 'text',
                    'text': 'cached',
                    'cache_control': {'type': 'ephemeral'},
                },
                {
                    'type': 'text',
                    'text': 'ignored',
                },
            ],
        )


if __name__ == '__main__':
    unittest.main()
