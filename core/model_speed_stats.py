import json
import threading
import time
from pathlib import Path


class ModelSpeedStats:
    def __init__(self, path: str = 'data/model_speed_stats.json'):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = {'models': {}}
        self._load()

    def _load(self):
        try:
            with self.path.open('r', encoding='utf-8') as fp:
                data = json.load(fp)
            if isinstance(data, dict) and isinstance(data.get('models'), dict):
                self._data = data
        except (OSError, ValueError, TypeError):
            self._data = {'models': {}}

    def record(self, *, model: str, endpoint: str, total_ms: float, first_token_ms: float | None):
        key = f'{model} @ {endpoint}'
        with self._lock:
            item = self._data.setdefault('models', {}).setdefault(key, {
                'model': model,
                'endpoint': endpoint,
                'calls': 0,
                'total_ms': 0.0,
                'first_token_calls': 0,
                'first_token_ms': 0.0,
                'updated_at': 0.0,
            })
            item['calls'] += 1
            item['total_ms'] += max(0.0, float(total_ms))
            if first_token_ms is not None:
                item['first_token_calls'] += 1
                item['first_token_ms'] += max(0.0, float(first_token_ms))
            item['updated_at'] = time.time()
            self._save_locked()

    def _save_locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            with temp.open('w', encoding='utf-8') as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2)
            temp.replace(self.path)
        except OSError:
            pass

    def snapshot(self) -> list[dict]:
        with self._lock:
            result = []
            for item in self._data.get('models', {}).values():
                calls = int(item.get('calls') or 0)
                first_calls = int(item.get('first_token_calls') or 0)
                result.append({
                    'model': item.get('model', ''),
                    'endpoint': item.get('endpoint', ''),
                    'calls': calls,
                    'avg_total_ms': (float(item.get('total_ms') or 0.0) / calls) if calls else 0.0,
                    'first_token_calls': first_calls,
                    'avg_first_token_ms': (float(item.get('first_token_ms') or 0.0) / first_calls) if first_calls else None,
                })
            return sorted(result, key=lambda x: (x['model'], x['endpoint']))

    def format_text(self) -> str:
        rows = self.snapshot()
        if not rows:
            return '暂无模型速度统计。'
        lines = ['模型速度统计', '统计口径：首字延迟为收到首个文本或工具输出增量的耗时，平均时长为完整请求耗时。', '']
        for row in rows:
            first = f'{row["avg_first_token_ms"]:.0f} ms' if row['avg_first_token_ms'] is not None else '暂无'
            lines.extend([
                f'模型: {row["model"]}',
                f'接口: {row["endpoint"]}',
                f'调用次数: {row["calls"]}',
                f'平均首字延迟: {first}',
                f'平均调用时长: {row["avg_total_ms"]:.0f} ms',
                '',
            ])
        return '\n'.join(lines).rstrip()
