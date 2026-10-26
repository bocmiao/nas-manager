"""带 TTL 的进程内缓存，避免高频请求把 NAS 打满。"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

Value = Any
Factory = Callable[[], Awaitable[Value]]


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Value]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_or_set(self, key: str, ttl: float, factory: Factory) -> Value:
        now = time.monotonic()
        hit = self._data.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = await factory()
            self._data[key] = (time.monotonic(), value)
            return value

    def peek(self, key: str) -> tuple[float, Value] | None:
        return self._data.get(key)

    def put(self, key: str, value: Value) -> None:
        """直接写入（用于缓存 get_or_set 之外的派生值，例如反查出来的最后登录时间）。"""
        self._data[key] = (time.monotonic(), value)

    def invalidate(self, prefix: str = "") -> None:
        for k in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(k, None)

    def stats(self) -> dict[str, float]:
        now = time.monotonic()
        return {k: round(now - ts, 1) for k, (ts, _) in self._data.items()}
