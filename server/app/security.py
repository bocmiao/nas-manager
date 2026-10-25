"""中间层对外鉴权与限流。

- Bearer Token 鉴权（token 来自配置，非 NAS 密码）
- 固定窗口限流，防爆破
- 失败次数过多的来源临时封禁
"""
from __future__ import annotations

import hmac
import logging
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

from .config import Settings

log = logging.getLogger("security")

WINDOW = 60.0
MAX_AUTH_FAILURES = 10
BAN_SECONDS = 300.0


class RateLimiter:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._banned: dict[str, float] = {}

    def _prune(self, bucket: list[float], now: float, window: float) -> None:
        while bucket and now - bucket[0] > window:
            bucket.pop(0)

    def check(self, ip: str) -> None:
        now = time.monotonic()
        until = self._banned.get(ip)
        if until and now < until:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"来源已被临时封禁，{int(until - now)} 秒后重试",
            )
        if until and now >= until:
            self._banned.pop(ip, None)

        bucket = self._hits[ip]
        self._prune(bucket, now, WINDOW)
        if len(bucket) >= self._s.rate_limit_per_min:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="请求过于频繁")
        bucket.append(now)

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        bucket = self._failures[ip]
        self._prune(bucket, now, WINDOW)
        bucket.append(now)
        if len(bucket) >= MAX_AUTH_FAILURES:
            self._banned[ip] = now + BAN_SECONDS
            log.warning("来源 %s 鉴权失败 %d 次，封禁 %d 秒", ip, len(bucket), int(BAN_SECONDS))

    def reset_failures(self, ip: str) -> None:
        self._failures.pop(ip, None)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def verify_token(request: Request, settings: Settings, limiter: RateLimiter) -> None:
    ip = client_ip(request)
    limiter.check(ip)

    header = request.headers.get("authorization", "")
    token = ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = request.headers.get("x-api-token", "").strip()

    expected = settings.api_token
    if not expected or not hmac.compare_digest(token, expected):
        limiter.record_failure(ip)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="无效的访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    limiter.reset_failures(ip)
