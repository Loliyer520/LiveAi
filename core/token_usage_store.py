import threading
import time
from copy import deepcopy

from pack.json_store import JsonStore


class TokenUsageStore:
    """Small, atomically persisted token counters shared by all model calls."""

    def __init__(self, path: str):
        self.store = JsonStore(path)
        self._lock = threading.RLock()
        self.store.update(self._ensure_shape)

    @staticmethod
    def _empty_counter() -> dict:
        return {
            'input_tokens': 0,
            'output_tokens': 0,
            'call_count': 0,
            'estimated_call_count': 0,
        }

    @classmethod
    def _ensure_shape(cls, data: dict) -> None:
        data.setdefault('version', 1)
        data.setdefault('global', cls._empty_counter())
        data.setdefault('scopes', {})
        data.setdefault('last', None)
        data.setdefault('last_by_scope', {})

    @classmethod
    def _add(cls, counter: dict, input_tokens: int, output_tokens: int, estimated: bool) -> None:
        defaults = cls._empty_counter()
        for key, value in defaults.items():
            counter.setdefault(key, value)
        counter['input_tokens'] += input_tokens
        counter['output_tokens'] += output_tokens
        counter['call_count'] += 1
        if estimated:
            counter['estimated_call_count'] += 1

    def record(self, input_tokens, output_tokens, estimated=False, model='', scope_key=None) -> dict | None:
        if input_tokens is None or output_tokens is None:
            return None
        try:
            input_tokens = max(0, int(input_tokens))
            output_tokens = max(0, int(output_tokens))
        except (TypeError, ValueError):
            return None
        scope_key = str(scope_key or '').strip() or None
        entry = {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'estimated': bool(estimated),
            'model': str(model or ''),
            'scope': scope_key,
            'updated_at': time.time(),
        }

        def mutate(data):
            self._ensure_shape(data)
            self._add(data['global'], input_tokens, output_tokens, bool(estimated))
            data['last'] = entry
            if scope_key:
                counter = data['scopes'].setdefault(scope_key, self._empty_counter())
                self._add(counter, input_tokens, output_tokens, bool(estimated))
                data['last_by_scope'][scope_key] = entry

        # JsonStore already serializes update/save; this lock also makes intent explicit.
        with self._lock:
            self.store.update(mutate)
        return deepcopy(entry)

    def snapshot(self, scope_key=None) -> dict:
        scope_key = str(scope_key or '').strip() or None
        with self._lock:
            data = deepcopy(self.store.load())
        self._ensure_shape(data)
        return {
            'global': data['global'],
            'scope': data['scopes'].get(scope_key) if scope_key else None,
            'last': (data['last_by_scope'].get(scope_key) if scope_key else None) or data['last'],
        }
