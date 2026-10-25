"""HTTP 路由。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..services.alerts import AlertEngine
from ..services.nas import NasService
from ..services.snapshot import build_overview, gather_snapshot
from ..ugos.client import UgosError
from .deps import get_alerts, get_nas, require_auth

router = APIRouter()
public = APIRouter()


def ok(data, **extra):
    return {"ok": True, "ts": int(time.time()), "data": data, **extra}


# ------------------------------------------------------------------ 公开

@public.get("/health")
async def health(request: Request):
    """供外部 uptime 监控使用，不含敏感信息。"""
    state = request.app.state
    return {
        "ok": True,
        "ts": int(time.time()),
        "data": {
            "server": "up",
            "version": state.version,
            "uptime_seconds": int(time.time() - state.started_at),
            "ugos": {
                "reachable": state.ugos_reachable,
                "authenticated": state.nas.has_session,
                "last_poll_ok": state.last_poll_ok,
                "last_poll_at": state.last_poll_at,
            },
        },
    }


# ------------------------------------------------------------------ 需要鉴权

@router.get("/capabilities", dependencies=[Depends(require_auth)])
async def capabilities(nas: NasService = Depends(get_nas)):
    return ok(await nas.capabilities())


@router.get("/system", dependencies=[Depends(require_auth)])
async def system(nas: NasService = Depends(get_nas)):
    return ok(await nas.system())


@router.get("/live", dependencies=[Depends(require_auth)])
async def live(nas: NasService = Depends(get_nas)):
    return ok(await nas.live())


@router.get("/overview", dependencies=[Depends(require_auth)])
async def overview(nas: NasService = Depends(get_nas), alerts: AlertEngine = Depends(get_alerts)):
    return ok(await build_overview(nas, alerts))


@router.get("/storage", dependencies=[Depends(require_auth)])
async def storage(nas: NasService = Depends(get_nas)):
    return ok(await nas.storage())


@router.get("/containers", dependencies=[Depends(require_auth)])
async def containers(nas: NasService = Depends(get_nas)):
    items = await nas.containers()
    return ok(
        {
            "total": len(items),
            "running": sum(1 for c in items if c.get("running")),
            "items": items,
        }
    )


@router.post("/containers/{container_id}/{action}", dependencies=[Depends(require_auth)])
async def container_action(
    container_id: str,
    action: str,
    nas: NasService = Depends(get_nas),
):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"不支持的操作：{action}")
    try:
        result = await nas.container_action(container_id, action)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ok(result)


@router.get("/containers/{container_id}/logs", dependencies=[Depends(require_auth)])
async def container_logs(
    container_id: str,
    tail: int = Query(200, ge=1, le=5000),
    nas: NasService = Depends(get_nas),
):
    return ok(await nas.container_logs(container_id, tail))


@router.get("/logs", dependencies=[Depends(require_auth)])
async def logs(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    level: str = "",
    module: str = "",
    keyword: str = "",
    nas: NasService = Depends(get_nas),
):
    return ok(await nas.logs(page, size, level, module, keyword))


@router.get("/alerts", dependencies=[Depends(require_auth)])
async def alerts_list(alerts: AlertEngine = Depends(get_alerts)):
    return ok(
        {
            "summary": alerts.summary(),
            "items": [a.to_dict() for a in alerts.active()],
        }
    )


@router.get("/alerts/history", dependencies=[Depends(require_auth)])
async def alerts_history(
    limit: int = Query(100, ge=1, le=500),
    alerts: AlertEngine = Depends(get_alerts),
):
    return ok([e.to_dict() for e in alerts.history(limit)])


@router.post("/alerts/containers/{name}/suppress", dependencies=[Depends(require_auth)])
async def suppress_container(
    name: str,
    enabled: bool = True,
    alerts: AlertEngine = Depends(get_alerts),
):
    alerts.suppress(name, enabled)
    return ok({"container": name, "suppressed": enabled})


@router.post("/alerts/poll", dependencies=[Depends(require_auth)])
async def poll_now(alerts: AlertEngine = Depends(get_alerts), nas: NasService = Depends(get_nas)):
    """手动触发一次巡检，便于联调。"""
    snapshot = await gather_snapshot(nas)
    events = await alerts.evaluate(snapshot)
    return ok({"events": [e.to_dict() for e in events], "summary": alerts.summary()})
