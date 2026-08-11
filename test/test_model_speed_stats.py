import json
import tempfile
import unittest
from pathlib import Path

from core.model_speed_stats import ModelSpeedStats


class ModelSpeedStatsTests(unittest.TestCase):
    def test_record_snapshot_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'speed.json'
            stats = ModelSpeedStats(str(path))
            stats.record(model='m1', endpoint='https://api/v1/responses', total_ms=100, first_token_ms=40)
            stats.record(model='m1', endpoint='https://api/v1/responses', total_ms=200, first_token_ms=60)
            stats.record(model='m1', endpoint='https://api/v1/responses', total_ms=50, first_token_ms=None)
            row = stats.snapshot()[0]
            self.assertEqual(row['calls'], 3)
            self.assertEqual(row['avg_total_ms'], 350 / 3)
            self.assertEqual(row['first_token_calls'], 2)
            self.assertEqual(row['avg_first_token_ms'], 50)
            self.assertIn('平均首字延迟: 50 ms', stats.format_text())

            reloaded = ModelSpeedStats(str(path))
            self.assertEqual(reloaded.snapshot()[0]['calls'], 3)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['models']['m1 @ https://api/v1/responses']['calls'], 3)

    def test_empty_stats_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = ModelSpeedStats(str(Path(tmp) / 'speed.json'))
            self.assertEqual(stats.format_text(), '暂无模型速度统计。')


if __name__ == '__main__':
    unittest.main()
