import json
import os
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path


class JsonStore:
    _REPLACE_ATTEMPTS = 5
    _REPLACE_BACKOFF = 0.05

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
            text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            # 临时名带 pid + 线程 + 随机段。固定的 .tmp 在有第二个写入者时（旧进程没退干净、
            # 多实例指向同一文件）会互相覆盖，也更容易撞上 Windows 的文件占用。
            suffix = f'{self.file_path.suffix}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp'
            temp_path = self.file_path.with_suffix(suffix)
            try:
                temp_path.write_text(text, encoding='utf-8')
                self._replace_with_retry(temp_path)
            finally:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
            # 落盘成功后才更新缓存：写失败时缓存留在旧值，与磁盘一致，
            # 否则调用方 load() 会拿到一份实际没存下来的数据，重启即丢。
            self._cache = payload

    def _replace_with_retry(self, temp_path: Path) -> None:
        """Windows 上目标被杀软/索引器/另一进程短暂持有句柄时 replace 会抛 WinError 32。"""
        for attempt in range(self._REPLACE_ATTEMPTS):
            try:
                temp_path.replace(self.file_path)
                return
            except OSError:
                if attempt + 1 >= self._REPLACE_ATTEMPTS:
                    raise
                time.sleep(self._REPLACE_BACKOFF * (attempt + 1))

    def update(self, mutator):
        with self._lock:
            payload = self.load()
            working = deepcopy(payload)
            result = mutator(working)
            self.save(working)
            return result
