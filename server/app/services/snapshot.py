"""快照聚合：把多个数据源合成一个稳定结构，供告警引擎与总览接口共用。"""
from __future__ import annotations

import asyncio
import logging
import time

from .alerts import AlertEngine
from .nas import NasService

log = logging.getLogger("snapshot")


async def gather_snapshot(nas: NasService) -> dict:
    """并发拉取，单个数据源失败不影响其它（该字段降级为空）。"""
    results = await asyncio.gather(
        nas.system(),
        nas.live(),
        nas.storage(),
        nas.containers(),
        return_exceptions=True,
    )
    names = ("system", "live", "storage", "containers")
    snapshot: dict = {}
    for name, value in zip(names, results):
        if isinstance(value, BaseException):
            log.warning("数据源 %s 失败：%s", name, value)
            snapshot[name] = [] if name == "containers" else {}
            snapshot.setdefault("degraded", []).append(name)
        else:
            snapshot[name] = value
    snapshot["ts"] = int(time.time())
    return snapshot


async def build_overview(nas: NasService, engine: AlertEngine, snapshot: dict | None = None) -> dict:
    snap = snapshot or await gather_snapshot(nas)
    system = snap.get("system") or {}
    live = snap.get("live") or {}
    storage = snap.get("storage") or {}
    containers = snap.get("containers") or []

    running = [c for c in containers if c.get("running")]
    stopped = [c for c in containers if not c.get("running")]

    volumes = storage.get("volumes") or []
    primary = max(volumes, key=lambda v: v.get("total") or 0) if volumes else None

    return {
        "device": {
            "name": system.get("name"),
            "model": system.get("model"),
            "firmware": system.get("firmware"),
            "uptime_seconds": system.get("uptime_seconds"),
            "boot_at": system.get("boot_at"),
        },
        "cpu": {
            **(system.get("cpu") or {}),
            "used_percent": (live.get("cpu") or {}).get("used_percent"),
            "temp": (live.get("cpu") or {}).get("temp"),
        },
        "memory": {
            "total": (system.get("memory") or {}).get("total"),
            "used_percent": (live.get("memory") or {}).get("used_percent"),
            "modules": (system.get("memory") or {}).get("modules") or [],
        },
        "fans": live.get("fans") or {},
        "nics": (system.get("nics") or []),
        "net": live.get("net") or {},
        "io": {"disk": live.get("disk_io") or {}, "volume": live.get("volume_io") or {}},
        "gpu": live.get("gpu") or {},
        "storage": {
            "pools": storage.get("pools") or [],
            "volume": primary,
            "volume_count": len(volumes),
            "disk_count": len(storage.get("disks") or []),
        },
        "containers": {
            "total": len(containers),
            "running": len(running),
            "stopped": len(stopped),
        },
        "alerts": engine.summary(),
        "degraded": snap.get("degraded") or [],
        "ts": snap.get("ts"),
    }
