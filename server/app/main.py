"""中间层入口。

启动：
    cd server && python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .api.routes import public, router
from .cache import TTLCache
from .config import get_settings
from .security import RateLimiter
from .services.alerts import AlertEngine
from .services.benchmark import BenchmarkManager
from .services.nas import NasService
from .services.push import PushService
from .services.snapshot import gather_snapshot
from .ugos.client import UgosAuthRequired, UgosClient, UgosError

log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    )
    # 每个请求都打一行太吵，只保留警告
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    app.state.settings = settings
    app.state.version = __version__
    app.state.started_at = time.time()
    app.state.limiter = RateLimiter(settings)
    app.state.cache = TTLCache()
    app.state.client = UgosClient(
        settings.ugos_base, settings.ugos_user, settings.ugos_password, settings.ugos_timeout
    )
    app.state.nas = NasService(app.state.client, app.state.cache, settings)
    app.state.alerts = AlertEngine(settings)
    app.state.benchmark = BenchmarkManager(app.state.nas, settings)
    app.state.push = PushService(settings)
    app.state.ugos_reachable = False
    app.state.last_poll_ok = False
    app.state.last_poll_at = 0

    configured_token = settings.api_token
    token = settings.resolve_api_token()

    # 首次登录：失败不阻断启动，由 /health 暴露状态
    try:
        await app.state.client.login()
        app.state.ugos_reachable = True
        log.info("已连接 NAS %s（%s）", settings.ugos_host, settings.ugos_user)
    except UgosError as exc:
        log.error("首次登录失败：%s", exc)

    if not configured_token:
        log.warning("=" * 60)
        log.warning("未配置 API_TOKEN，本次使用临时令牌（重启失效）：")
        log.warning("    %s", token)
        log.warning("请写入 .env： API_TOKEN=%s", token)
        log.warning("=" * 60)

    if settings.apns_configured:
        log.info("APNs 推送已启用 topic=%s host=%s", settings.apns_topic, settings.apns_host)
    else:
        log.warning("未配置 APNs 凭据，告警只写日志不推送")

    async def poller() -> None:
        while True:
            try:
                snapshot = await gather_snapshot(app.state.nas)
                events = await app.state.alerts.evaluate(snapshot)
                app.state.ugos_reachable = True
                app.state.last_poll_ok = True
                app.state.last_poll_at = int(time.time())
                if events:
                    log.info("巡检状态变化 %d 条", len(events))
                    for ev in events:
                        await app.state.push.send_alert(ev)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 巡检不能因单次失败退出
                app.state.last_poll_ok = False
                log.exception("巡检失败")
            await asyncio.sleep(max(10, settings.poll_interval))

    task = asyncio.create_task(poller())
    log.info("中间层就绪 %s:%d  版本 %s", settings.server_host, settings.server_port, __version__)

    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await app.state.benchmark.shutdown()
        await app.state.push.close()
        await app.state.client.close()
        log.info("中间层已停止")


app = FastAPI(
    title="NasManager Middleware",
    description="绿联 UGOS Pro 的 iOS 管理端中间层。所有能力经统一契约对 App 暴露。",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)
app.include_router(public)
app.include_router(router, prefix="/api/v1")


@app.exception_handler(UgosError)
async def handle_ugos_error(request: Request, exc: UgosError) -> JSONResponse:
    code = 503 if isinstance(exc, UgosAuthRequired) else 502
    log.warning("UGOS 错误 %s", exc)
    return JSONResponse(
        status_code=code,
        content={
            "ok": False,
            "ts": int(time.time()),
            "error": {"code": exc.code, "message": exc.msg, "path": exc.path},
        },
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("未处理异常：%s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "ts": int(time.time()),
                 "error": {"code": -1, "message": "中间层内部错误"}},
    )


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.server_host, port=s.server_port, log_level=s.log_level.lower())
