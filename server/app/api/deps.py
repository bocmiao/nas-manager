"""FastAPI 依赖注入。"""
from __future__ import annotations

from fastapi import Request

from ..security import verify_token
from ..services.alerts import AlertEngine
from ..services.backup import BackupService
from ..services.benchmark import BenchmarkManager
from ..services.fans import FanService
from ..services.health import HealthService
from ..services.history import HistoryRecorder
from ..services.images import ImageManager
from ..services.logs import LogService
from ..services.nas import NasService
from ..services.power import DiskSleepMonitor
from ..services.powerplan import PowerPlanService
from ..services.push import PushService
from ..services.sessions import SessionService
from ..services.snapshots import SnapshotService
from ..services.users import UserService
from ..services.vms import VmService


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


def get_images(request: Request) -> ImageManager:
    return request.app.state.images


def get_snapshots(request: Request) -> SnapshotService:
    return request.app.state.snapshots


def get_history(request: Request) -> HistoryRecorder:
    return request.app.state.history


def get_users(request: Request) -> UserService:
    return request.app.state.users


def get_health(request: Request) -> HealthService:
    return request.app.state.health


def get_sleep(request: Request) -> DiskSleepMonitor:
    return request.app.state.disk_sleep


def get_logs(request: Request) -> LogService:
    return request.app.state.logs


def get_sessions(request: Request) -> SessionService:
    return request.app.state.sessions


def get_fans(request: Request) -> FanService:
    return request.app.state.fans


def get_backup(request: Request) -> BackupService:
    return request.app.state.backup


def get_power_plan(request: Request) -> PowerPlanService:
    return request.app.state.power_plan


def get_vms(request: Request) -> VmService:
    return request.app.state.vms
