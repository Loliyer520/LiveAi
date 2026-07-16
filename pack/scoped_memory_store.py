import re
import threading
from pathlib import Path

from pack.json_store import JsonStore


_SAFE_RE = re.compile(r'[^A-Za-z0-9_-]')


def scope_filename(scope_key: str) -> str:
    """把 scope key (形如 'group:123' / 'private:456' / 'master:global')
    映射成安全的文件名。scope_type 与 scope_id 用双下划线分隔，
    scope_id 内非 [A-Za-z0-9_-] 的字符替换为下划线。"""
    if ':' in scope_key:
        scope_type, scope_id = scope_key.split(':', 1)
    else:
        scope_type, scope_id = 'unknown', scope_key
    safe_type = _SAFE_RE.sub('_', scope_type)
    safe_id = _SAFE_RE.sub('_', scope_id)
    return f'{safe_type}__{safe_id}.json'


class ScopedMemoryStore:
    """按 scope 拆分的 memory 存储管理器。

    每个 scope 的 memory（{messages, notes, tool_logs, turn_logs}）独立存成
    一个小 JSON 文件，放在 base_dir 下。内部按 scope_key 惰性创建并缓存
    JsonStore 实例，复用 JsonStore 的原子写、缓存与 RLock 语义。

    这样对某个 scope 的写只会 deepcopy + 落盘该 scope 的小文件，
    不再重写其他 scope 的数据。
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stores: dict[str, JsonStore] = {}

    def _store_for(self, scope_key: str) -> JsonStore:
        with self._lock:
            store = self._stores.get(scope_key)
            if store is None:
                path = self.base_dir / scope_filename(scope_key)
                store = JsonStore(str(path))
                self._stores[scope_key] = store
            return store

    def load(self, scope_key: str) -> dict:
        """返回该 scope 的 memory dict（缓存引用，调用方勿原地改）。
        新 scope 首次访问时文件内容为空 {}。"""
        return self._store_for(scope_key).load()

    def update(self, scope_key: str, mutator):
        """对单个 scope 的 memory 做 deepcopy+mutator+落盘。
        mutator 收到该 scope 的 memory dict（可能是空 {}）。"""
        return self._store_for(scope_key).update(mutator)

    def list_scopes(self) -> list[str]:
        """扫描 base_dir 下所有 memory 文件，返回文件名列表（不含反解 key）。
        排除下划线开头的元文件（如 _index.json）。"""
        return [p.name for p in self.base_dir.glob('*.json') if not p.name.startswith('_')]

    def reset_all(self):
        """清空所有 scope 的 memory：删除全部文件并清空实例缓存。"""
        with self._lock:
            for path in self.base_dir.glob('*.json'):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self._stores.clear()
