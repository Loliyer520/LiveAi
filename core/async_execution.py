from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar


T = TypeVar('T')


class AsyncExecutionPool:
    """Named thread pool isolated from asyncio's shared default executor."""

    def __init__(self, name: str, max_workers: int) -> None:
        name = str(name or '').strip()
        if not name:
            raise ValueError('name must be non-empty')
        if max_workers < 1:
            raise ValueError('max_workers must be positive')
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=name,
        )
        self._closed = False

    @property
    def executor(self) -> ThreadPoolExecutor:
        if self._closed:
            raise RuntimeError('execution pool is closed')
        return self._executor

    async def run(self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        if self._closed:
            raise RuntimeError('execution pool is closed')
        if not callable(func):
            raise TypeError('func must be callable')
        loop = asyncio.get_running_loop()
        call = functools.partial(func, *args, **kwargs)
        return await loop.run_in_executor(self._executor, call)

    def close(self, *, wait: bool = False, cancel_futures: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
