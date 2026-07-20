import json
import threading
from copy import deepcopy
from pathlib import Path


class JsonStore:
    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache = None
        if not self.file_path.exists():
            self.file_path.write_text('{}', encoding='utf-8')

    def load(self) -> dict:
        with self._lock:
            if self._cache is not None:
                return self._cache
            raw = self.file_path.read_text(encoding='utf-8').strip() or '{}'
            decoder = json.JSONDecoder()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload, end = decoder.raw_decode(raw)
                extra = raw[end:].strip()
                if extra:
                    # Heal files that contain a valid JSON document followed by junk.
                    self.save(payload)
            self._cache = payload
            return payload

    def save(self, payload: dict):
        with self._lock:
            self._cache = payload
            temp_path = self.file_path.with_suffix(self.file_path.suffix + '.tmp')
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            temp_path.replace(self.file_path)

    def update(self, mutator):
        with self._lock:
            payload = self.load()
            working = deepcopy(payload)
            result = mutator(working)
            self.save(working)
            return result
