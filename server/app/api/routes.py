"""HTTP 路由。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from ..services.alerts import AlertEngine
from ..services.benchmark import BenchmarkManager, BenchmarkRejected
from ..services.history import HistoryRecorder
from ..services.images import ImageManager
from ..services.nas import NasService
from ..services.push import PushService
from ..services.snapshot import build_overview, gather_snapshot
from ..services.snapshots import SnapshotService
from .deps import (
    get_alerts,
    get_benchmark,
    get_history,
    get_images,
    get_nas,
    get_push,
    get_snapshots,
    require_auth,
)

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
            "push": {
                "configured": state.push.stats()["configured"],
                "devices": state.push.stats()["devices"],
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


@router.get("/firmware", dependencies=[Depends(require_auth)])
async def firmware(nas: NasService = Depends(get_nas)):
    """固件版本查询（只读）。中间层不提供升级动作。"""
    return ok(await nas.firmware())


@router.get("/ups", dependencies=[Depends(require_auth)])
async def ups(nas: NasService = Depends(get_nas)):
    """UPS 状态。未接 UPS 时返回 `supported: false`。"""
    return ok(await nas.ups())


# ------------------------------------------------------------------ 历史曲线

@router.get("/history", dependencies=[Depends(require_auth)])
async def history(
    metric: str = Query("cpu"),
    points: int = Query(120, ge=1, le=3600),
    disk: str = Query("", description="metric=disk_temp 时指定盘名或标签"),
    hist: HistoryRecorder = Depends(get_history),
):
    """内存环形缓冲里的历史采样。

    `since` 是缓冲里最早一个点的真实时间戳——服务重启后缓冲清空，它会变成"现在"，
    客户端不要假设它固定是 6 小时前。
    """
    try:
        return ok(hist.query(metric, points, disk))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/history/metrics", dependencies=[Depends(require_auth)])
async def history_metrics(hist: HistoryRecorder = Depends(get_history)):
    """可用指标清单与缓冲状态。"""
    return ok(hist.metrics())


@router.get("/overview", dependencies=[Depends(require_auth)])
async def overview(nas: NasService = Depends(get_nas), alerts: AlertEngine = Depends(get_alerts)):
    return ok(await build_overview(nas, alerts))


@router.get("/storage", dependencies=[Depends(require_auth)])
async def storage(nas: NasService = Depends(get_nas)):
    return ok(await nas.storage())


# ------------------------------------------------------------------ 磁盘 SMART / 测速

@router.get("/storage/disks/{disk}/smart", dependencies=[Depends(require_auth)])
async def disk_smart(disk: str, nas: NasService = Depends(get_nas)):
    """SMART 明细。

    `disk` 用盘名（sda）、标签（硬盘1）或序列号均可。
    注意 `/dev/sda` 这种带斜杠的写法在路径里无法匹配（会被当成多级路由），
    请用盘名或标签。
    """
    try:
        return ok(await nas.disk_smart(disk))
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/storage/disks/{disk}/state", dependencies=[Depends(require_auth)])
async def disk_state(disk: str, nas: NasService = Depends(get_nas)):
    try:
        return ok(await nas.disk_state(disk))
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/storage/disks/{disk}/benchmark", status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(require_auth)])
async def benchmark_start(
    disk: str,
    nas: NasService = Depends(get_nas),
    bench: BenchmarkManager = Depends(get_benchmark),
):
    """发起磁盘测速（异步任务，立刻返回任务状态）。"""
    try:
        return ok(await bench.start(disk))
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except BenchmarkRejected as exc:
        # NAS 侧业务拒绝（磁盘在用 / 忙），不是中间层故障
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "hint": exc.hint},
        ) from exc


@router.get("/storage/disks/{disk}/benchmark", dependencies=[Depends(require_auth)])
async def benchmark_status(
    disk: str,
    nas: NasService = Depends(get_nas),
    bench: BenchmarkManager = Depends(get_benchmark),
):
    try:
        info = await nas.resolve_disk(disk)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    dev = f"/dev/{info.get('name')}"
    job = bench.status(dev)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{dev} 没有测速任务记录")
    return ok(job)


@router.delete("/storage/disks/{disk}/benchmark", dependencies=[Depends(require_auth)])
async def benchmark_stop(disk: str, bench: BenchmarkManager = Depends(get_benchmark)):
    try:
        return ok(await bench.stop(disk))
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/storage/disks/{disk}/benchmark/history", dependencies=[Depends(require_auth)])
async def benchmark_history(disk: str, bench: BenchmarkManager = Depends(get_benchmark)):
    try:
        return ok(await bench.history(disk))
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# ------------------------------------------------------------------ 容器

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
    keyword: str = "",
    nas: NasService = Depends(get_nas),
):
    """容器日志。

    注意：UGOS 会忽略行数参数并返回完整日志，`tail` 由中间层本地截断，
    响应里的 `total_lines` 是真实总行数。
    """
    try:
        return ok(await nas.container_logs(container_id, tail, keyword))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/containers/{container_id}/detail", dependencies=[Depends(require_auth)])
async def container_detail(container_id: str, nas: NasService = Depends(get_nas)):
    """容器详情：环境变量 / 端口映射 / 卷 / 网络 / 资源限制。

    compose 内容拿不到，响应里的 `compose.available=false` 会说明原因。
    """
    try:
        return ok(await nas.container_detail(container_id))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# ------------------------------------------------------------------ 镜像

@router.get("/docker/images", dependencies=[Depends(require_auth)])
async def docker_images(images: ImageManager = Depends(get_images)):
    """本地镜像列表，含大小、创建时间、被哪些容器引用。"""
    return ok(await images.list_images())


@router.delete("/docker/images", dependencies=[Depends(require_auth)])
async def docker_images_delete(
    ids: list[str] = Query(..., description="镜像 id / name:tag / 镜像名，可重复"),
    images: ImageManager = Depends(get_images),
):
    """删除镜像：传 1 个走 DeleteImage，多个走 BatchDeleteImage。"""
    try:
        return ok(await images.delete(ids))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/docker/images/pull", status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(require_auth)])
async def docker_images_pull(
    name: str = Body(..., embed=True),
    tag: str = Body("latest", embed=True),
    images: ImageManager = Depends(get_images),
):
    """发起镜像拉取（异步任务，立刻返回任务状态）。

    UGOS 没有字节级进度：拉取期间镜像列表里会出现 `<name>:<tag>:pull`、`status=2`，
    完成后变成 `status=1`。轮询 `/docker/images/pull?ref=` 看状态。
    """
    try:
        return ok(await images.pull(name, tag))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.get("/docker/images/pulls", dependencies=[Depends(require_auth)])
async def docker_image_pulls(images: ImageManager = Depends(get_images)):
    """本进程发起的全部拉取任务。"""
    return ok({"total": len(images.jobs()), "items": images.jobs()})


@router.get("/docker/images/pull", dependencies=[Depends(require_auth)])
async def docker_image_pull_status(
    ref: str = Query(..., description="name:tag"),
    images: ImageManager = Depends(get_images),
):
    job = images.job_status(ref)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"没有 {ref} 的拉取任务")
    return ok(job)


@router.get("/docker/usage", dependencies=[Depends(require_auth)])
async def docker_usage(nas: NasService = Depends(get_nas)):
    """Docker 引擎 + 每个容器的 CPU / 内存 / 网络累计流量。"""
    return ok(await nas.docker_usage())


# ------------------------------------------------------------------ 快照

@router.get("/snapshots", dependencies=[Depends(require_auth)])
async def snapshots_overview(snap: SnapshotService = Depends(get_snapshots)):
    """快照总览：能力、卷文件系统、各类型的快照文件夹。

    非 Btrfs 卷上 `supported=false`，`reason` 会写明原因。
    """
    return ok(await snap.overview())


@router.get("/snapshots/{folder_type}", dependencies=[Depends(require_auth)])
async def snapshot_folders(folder_type: str, snap: SnapshotService = Depends(get_snapshots)):
    """某类型下的快照文件夹列表（folder_type = share / home）。"""
    try:
        return ok(await snap.folders(folder_type))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/snapshots/{folder_type}/{folder_id}", dependencies=[Depends(require_auth)])
async def snapshot_list(
    folder_type: str,
    folder_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    snap: SnapshotService = Depends(get_snapshots),
):
    """某文件夹的快照列表。"""
    try:
        return ok(await snap.snapshots(folder_type, folder_id, page, size))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/snapshots/{folder_type}/{folder_id}/schedule",
            dependencies=[Depends(require_auth)])
async def snapshot_schedule(
    folder_type: str, folder_id: int, snap: SnapshotService = Depends(get_snapshots)
):
    """快照计划与保留策略（只读）。"""
    try:
        return ok(await snap.schedule(folder_type, folder_id))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/snapshots/{folder_type}/{folder_id}", dependencies=[Depends(require_auth)])
async def snapshot_create(
    folder_type: str,
    folder_id: int,
    comment: str = Body("", embed=True),
    snap: SnapshotService = Depends(get_snapshots),
):
    """立即创建一个快照。"""
    try:
        return ok(await snap.create(folder_type, folder_id, comment))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/snapshots/{folder_type}/{folder_id}", dependencies=[Depends(require_auth)])
async def snapshot_delete(
    folder_type: str,
    folder_id: int,
    names: list[str] = Query(..., description="快照名，可重复"),
    snap: SnapshotService = Depends(get_snapshots),
):
    """删除快照（支持批量）。UGOS 对不存在的快照也返回 success，故带 `verified` 标记。"""
    try:
        return ok(await snap.delete(folder_type, folder_id, names))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/snapshots/{folder_type}/{folder_id}/restore",
             dependencies=[Depends(require_auth)])
async def snapshot_restore(
    folder_type: str,
    folder_id: int,
    name: str = Body(..., embed=True),
    confirm: str = Body("", embed=True),
    snap: SnapshotService = Depends(get_snapshots),
):
    """回滚到指定快照。

    危险操作：必须显式传 `confirm="ROLLBACK"`，否则 400 拒绝，不会默认执行。
    """
    try:
        return ok(await snap.restore(folder_type, folder_id, name, confirm))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


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


# ------------------------------------------------------------------ 任务管理器

@router.get("/processes", dependencies=[Depends(require_auth)])
async def processes(
    limit: int = Query(50, ge=1, le=500),
    sort: str = Query("cpu", pattern="^(cpu|mem)$"),
    nas: NasService = Depends(get_nas),
):
    """进程列表（只读）。排序维度 cpu / mem。"""
    return ok(await nas.processes(limit, sort))


@router.get("/services", dependencies=[Depends(require_auth)])
async def services(nas: NasService = Depends(get_nas)):
    """系统服务列表（只读，不含启停操作）。"""
    return ok(await nas.services())


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
async def poll_now(alerts: AlertEngine = Depends(get_alerts), nas: NasService = Depends(get_nas),
                   push: PushService = Depends(get_push)):
    """手动触发一次巡检，便于联调。"""
    snapshot = await gather_snapshot(nas)
    events = await alerts.evaluate(snapshot)
    pushed = 0
    for ev in events:
        pushed += await push.send_alert(ev)
    return ok(
        {
            "events": [e.to_dict() for e in events],
            "summary": alerts.summary(),
            "pushed": pushed,
        }
    )


@router.get("/alerts/rules", dependencies=[Depends(require_auth)])
async def alert_rules(request: Request, alerts: AlertEngine = Depends(get_alerts)):
    """当前生效的告警规则与阈值，便于 App 端展示与排查误报。"""
    s = request.app.state.settings
    return ok(
        {
            "rules": [
                {"key": "disk_temp", "level": "warning", "threshold": s.alert_disk_temp,
                 "critical_at": s.alert_disk_temp_critical, "unit": "°C"},
                {"key": "disk_status", "level": "critical", "threshold": None},
                {"key": "disk_smart", "level": "warning",
                 "threshold": {
                     "reallocated_sectors": s.alert_smart_reallocated,
                     "pending_sectors": s.alert_smart_pending,
                     "offline_uncorrectable": s.alert_smart_offline_uncorrectable,
                 }},
                {"key": "disk_smart_crc", "level": "info", "threshold": ">0"},
                {"key": "pool_status", "level": "critical", "threshold": None},
                {"key": "pool_no_redundancy", "level": "info", "threshold": None},
                {"key": "volume_used", "level": "warning", "threshold": s.alert_volume_used,
                 "critical_at": s.alert_volume_used_critical, "unit": "%"},
                {"key": "cpu_temp", "level": "critical", "threshold": s.alert_cpu_temp, "unit": "°C"},
                {"key": "cpu_used", "level": "warning", "threshold": s.alert_cpu_used, "unit": "%"},
                {"key": "mem_used", "level": "warning", "threshold": s.alert_mem_used, "unit": "%"},
                {"key": "fan_stall", "level": "warning", "threshold": s.alert_fan_min_rpm, "unit": "RPM"},
                {"key": "ups_on_battery", "level": "critical", "threshold": None},
                {"key": "container_down", "level": "warning", "threshold": "连续 2 轮非运行"},
                {"key": "container_restart_loop", "level": "warning",
                 "threshold": f"{s.alert_container_restart_count} 次 / {s.alert_container_restart_window}s"},
                {"key": "nic_speed", "level": "warning",
                 "threshold": s.alert_nic_min_mbps or "低于历史峰值", "unit": "Mbps"},
            ],
            "summary": alerts.summary(),
        }
    )


# ------------------------------------------------------------------ 推送

@router.get("/push/devices", dependencies=[Depends(require_auth)])
async def push_devices(push: PushService = Depends(get_push)):
    return ok({"stats": push.stats(), "items": push.devices()})


@router.post("/push/devices", dependencies=[Depends(require_auth)])
async def push_register(
    token: str = Body(..., embed=True),
    platform: str = Body("ios", embed=True),
    name: str = Body("", embed=True),
    push: PushService = Depends(get_push),
):
    """注册 APNs 设备 token（内存存储，进程重启后需重新注册）。"""
    try:
        return ok(push.register(token, platform, name))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/push/devices/{token}", dependencies=[Depends(require_auth)])
async def push_unregister(token: str, push: PushService = Depends(get_push)):
    if not push.unregister(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="设备未注册")
    return ok({"removed": True})


@router.post("/push/test", dependencies=[Depends(require_auth)])
async def push_test(push: PushService = Depends(get_push)):
    """发一条测试推送，用于验证证书与设备 token。"""
    sent = await push.push(
        {
            "aps": {
                "alert": {"title": "测试推送", "body": "中间层推送通道连通性测试"},
                "sound": "default",
            },
            "test": True,
        }
    )
    return ok({"delivered": sent, "stats": push.stats()})
