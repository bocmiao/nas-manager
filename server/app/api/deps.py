"""FastAPI 依赖注入。"""
from __future__ import annotations

from fastapi import Request

from ..security import verify_token
from ..services.alerts import AlertEngine
from ..services.benchmark import BenchmarkManager
from ..services.nas import NasService
from ..services.push import PushService


def require_auth(request: Request) -> None:
    verify_token(request, request.app.state.settings, request.app.state.limiter)


def get_nas(request: Request) -> NasService:
    return request.app.state.nas


def get_alerts(request: Request) -> AlertEngine:
    return request.app.state.alerts


def get_benchmark(request: Request) -> BenchmarkManager:
    return request.app.state.benchmark


def get_push(request: Request) -> PushService:
    return request.app.state.push
