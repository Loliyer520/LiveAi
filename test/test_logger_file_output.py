import json
import tempfile
import unittest
from pathlib import Path

from core.logger import BotLogger, CAT_AGENT


class LoggerFileOutputTests(unittest.TestCase):
    def test_bot_logger_appends_local_jsonl_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / 'bot_debug.jsonl'
            logger = BotLogger(max_entries=100, log_file_path=log_path)

            logger.info(CAT_AGENT, 'ssh:prod', 'ssh 连接开始')
            logger.warn(CAT_AGENT, 'ssh:prod', 'ssh 连接超时')

            self.assertTrue(log_path.exists())
            lines = log_path.read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 2)

            first = json.loads(lines[0])
            second = json.loads(lines[1])
            self.assertEqual(first['category'], CAT_AGENT)
            self.assertEqual(first['scope_key'], 'ssh:prod')
            self.assertEqual(first['message'], 'ssh 连接开始')
            self.assertEqual(second['level'], 'warn')
            self.assertEqual(logger.get_log_file_path(), str(log_path))


if __name__ == '__main__':
    unittest.main()
