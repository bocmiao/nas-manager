"""FastAPI 依赖注入。"""
from __future__ import annotations

from fastapi import Request

from ..security import verify_token
from ..services.alerts import AlertEngine
from ..services.nas import NasService


def require_auth(request: Request) -> None:
    verify_token(request, request.app.state.settings, request.app.state.limiter)


def get_nas(request: Request) -> NasService:
    return request.app.state.nas


def get_alerts(request: Request) -> AlertEngine:
    return request.app.state.alerts
