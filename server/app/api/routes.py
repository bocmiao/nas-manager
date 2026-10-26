"""HTTP 路由。"""
from __future__ import annotations

import logging
import posixpath
import time
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from ..services.alerts import AlertEngine
from ..services.backup import BackupService
from ..services.benchmark import BenchmarkManager, BenchmarkRejected
from ..services.fans import FanService
from ..services.files import FileRejected, FileService
from ..services.health import HealthService
from ..services.history import HistoryRecorder
from ..services.images import ImageManager
from ..services.logs import LogService
from ..services.nas import NasService
from ..services.power import DiskSleepMonitor
from ..services.powerplan import PowerPlanService
from ..services.push import PushService
from ..services.sessions import SessionService
from ..services.snapshot import build_overview, gather_snapshot
from ..services.snapshots import SnapshotService
from ..services.users import UserService
from ..services.vms import VmService
from .deps import (
    get_alerts,
    get_files,
    get_backup,
    get_benchmark,
    get_fans,
    get_health,
    get_history,
    get_images,
    get_logs,
    get_nas,
    get_power_plan,
    get_push,
    get_sessions,
    get_sleep,
    get_snapshots,
    get_users,
    get_vms,
    require_auth,
)

log = logging.getLogger("api")

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
                # 本轮取不到的数据源，便于排障：全空但 reachable=false 说明整机/会话不可用
                "degraded": state.degraded,
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


# ------------------------------------------------------------------ 用户与共享（只读）

@router.get("/users", dependencies=[Depends(require_auth)])
async def users(svc: UserService = Depends(get_users)):
    """用户列表：UID / 角色 / 是否管理员 / 所属组 / 最后登录时间。

    只读。最后登录时间由审计日志 `module=login` 反查（用户接口本身不返回登录时间）。
    """
    return ok(await svc.users())


@router.get("/users/{uid}", dependencies=[Depends(require_auth)])
async def user_detail(uid: int, svc: UserService = Depends(get_users)):
    """单个用户详情：共享权限、配额、应用授权。"""
    try:
        return ok(await svc.detail(uid))
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/groups", dependencies=[Depends(require_auth)])
async def groups(svc: UserService = Depends(get_users)):
    """用户组列表：自定义组 + 从用户归纳出的内置组（含成员的共享权限）。"""
    return ok(await svc.groups())


@router.get("/shares", dependencies=[Depends(require_auth)])
async def shares(svc: UserService = Depends(get_users)):
    """共享文件夹与权限掩码。"""
    return ok(await svc.shares())


# ------------------------------------------------------------------ 在线会话与登录审计

@router.get("/sessions", dependencies=[Depends(require_auth)])
async def sessions_overview(svc: SessionService = Depends(get_sessions)):
    """在线会话 + 窗口内失败统计 + NAS 自带防爆破策略（一屏）。

    来源 IP 属敏感信息：接口按用户自己的需要如实返回，文档与示例里一律用 192.0.2.10 占位。
    """
    return ok(await svc.overview())


@router.get("/sessions/online", dependencies=[Depends(require_auth)])
async def sessions_online(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    svc: SessionService = Depends(get_sessions),
):
    """当前在线会话（`POST v1/user/online/list`）。

    字段：用户名 / 来源 IP / 接入地址 / 登录时间 / 客户端平台 / 协议 / 连接时长。
    支持踢下线的 `v1/user/online/stop` 存在但**不实现**（强制下线属高危操作）。
    """
    return ok(await svc.online(page, size))


@router.get("/sessions/logins", dependencies=[Depends(require_auth)])
async def sessions_logins(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    only_failed: bool = False,
    user: str = "",
    since: int | None = Query(None, description="Unix 秒，含"),
    until: int | None = Query(None, description="Unix 秒，含"),
    svc: SessionService = Depends(get_sessions),
):
    """登录审计日志（成功 / 失败 / 来源 IP / 方式），支持分页与只看失败。

    数据来自 `v1/log/query?module=login`：成功是 info、密码错误是 warning，
    所以 `only_failed=true` 用服务端级别过滤，分页 total 与筛选结果一致。
    """
    return ok(await svc.logins(page, size, only_failed, user, since, until))


@router.get("/sessions/failures", dependencies=[Depends(require_auth)])
async def sessions_failures(
    window: int = Query(0, ge=0, le=86400, description="统计窗口秒数，0 = 用配置默认值"),
    svc: SessionService = Depends(get_sessions),
):
    """窗口内按来源 IP 聚合的登录失败次数（告警规则的数据源）。"""
    return ok(await svc.failures(window or None))


@router.get("/security/faillock", dependencies=[Depends(require_auth)])
async def security_faillock(svc: SessionService = Depends(get_sessions)):
    """NAS 自带的失败锁定策略与黑名单（只读，中间层不修改）。"""
    return ok(await svc.faillock())


# ------------------------------------------------------------------ 网络

@router.get("/network/interfaces", dependencies=[Depends(require_auth)])
async def network_interfaces(
    health: HealthService = Depends(get_health),
    hist: HistoryRecorder = Depends(get_history),
):
    """网口详情：协商速率 / 双工 / MTU / 瞬时速率 / 累计流量估算。

    UGOS 不暴露错误包、丢弃包与累计流量，`counters.supported=false` 会说明原因；
    `traffic_estimate` 是中间层按自己采样积分出来的估算值，不是 NAS 计数。
    """
    return ok(await health.network(hist))


@router.get("/network/interfaces/{name}", dependencies=[Depends(require_auth)])
async def network_interface(
    name: str,
    health: HealthService = Depends(get_health),
    hist: HistoryRecorder = Depends(get_history),
):
    data = await health.network(hist)
    port = next((p for p in data.get("ports") or [] if p.get("name") == name), None)
    if port is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"没有网口 {name}")
    return ok({**port, "traffic_estimate": data.get("traffic_estimate"),
               "counters": data.get("counters")})


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


@router.get("/storage/health", dependencies=[Depends(require_auth)])
async def storage_health(health: HealthService = Depends(get_health)):
    """存储健康汇总：池 RAID 重建/校验进度、卷容量与 inode 支持情况。

    inode 在 UGOS 上拿不到，`volumes[].inode.supported=false` + `reason` 说明试过哪些接口。
    """
    return ok(await health.storage())


@router.get("/storage/disk-sleep", dependencies=[Depends(require_auth)])
async def disk_sleep(
    nas: NasService = Depends(get_nas),
    sleep: DiskSleepMonitor = Depends(get_sleep),
    alerts: AlertEngine = Depends(get_alerts),
):
    """硬盘休眠状态与频繁唤醒统计。

    状态唯一来源是磁盘列表的 `is_standby`（active / sleeping）；
    唤醒次数由中间层按巡检采样累计，采样间隔内「睡下又醒来」会漏计。
    """
    disks = await nas.disk_sleep_states()
    try:
        policy = await nas.disk_sleep_policy()
    except Exception:  # noqa: BLE001 - 策略接口不可用不影响状态
        policy = {}
    sleep.observe(disks, time.time())
    data = sleep.status(disks, policy)
    data["alerts"] = [
        a.to_dict() for a in alerts.active() if a.key.startswith("disk_wake_freq:")
    ]
    return ok(data)


# ------------------------------------------------------------------ 风扇 / 电源计划（只读）

@router.get("/fans", dependencies=[Depends(require_auth)])
async def fans(svc: FanService = Depends(get_fans)):
    """风扇转速与策略模式（只读）。

    转速来自 `stat/overview` 的 `cpu_fan` / `device_fan`；模式来自 `v1/hardware/fan/config`
    （1=静音 2=自动 3=全速 4=自定义）。目标转速没有接口暴露，`target_rpm.supported=false`。
    **调速接口存在但中间层不实现**（改策略可能损伤硬件）。
    """
    return ok(await svc.status())


@router.get("/power/plan", dependencies=[Depends(require_auth)])
async def power_plan(svc: PowerPlanService = Depends(get_power_plan)):
    """定时开关机计划（只读）。

    计划表不在独立接口里，而在 `v1/hardware/power/config` 的 `time_switches`。
    不给写接口：误改计划会让 NAS 不按预期启停。
    """
    return ok(await svc.plan())


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
    alerts: AlertEngine = Depends(get_alerts),
    push: PushService = Depends(get_push),
):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"不支持的操作：{action}")
    try:
        result = await nas.container_action(container_id, action)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # 用户主动停止必须告知告警引擎，否则下一两轮巡检就会报「应运行但已停止（非用户主动停止）」
    name = result.get("container_name")
    events = alerts.suppress(name, action == "stop") if name else []
    pushed = 0
    for ev in events:
        pushed += await push.send_alert(ev)
    return ok(
        {
            **result,
            "suppressed": (action == "stop") if name else None,
            "alert_events": [e.to_dict() for e in events],
            "pushed": pushed,
        }
    )


@router.get("/containers/baseline", dependencies=[Depends(require_auth)])
async def containers_baseline(alerts: AlertEngine = Depends(get_alerts)):
    """容器「期望运行」基线。

    连续 `ALERT_CONTAINER_EXPECTED_POLLS`（默认 3）轮巡检都是 running 的容器
    会被标记为期望运行；之后停止且未被 `suppress` 时产生 `container_expected_down`。
    """
    return ok(alerts.container_baseline())


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


@router.get("/containers/{container_id}/access", dependencies=[Depends(require_auth)])
async def container_access(container_id: str, nas: NasService = Depends(get_nas)):
    """容器 Web UI 的候选访问地址。

    只拼地址，不做可达性探测——探测由客户端做（服务端探测容易变成 SSRF 入口，
    而且慢）。响应里的 `ports[].urls` 是候选，`remote_disabled` 为真表示
    NAS 层已禁止该端口的远程访问。
    """
    try:
        return ok(await nas.container_web_access(container_id))
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
    force: bool = Query(False, description="镜像仍被容器引用时是否强制删除"),
    images: ImageManager = Depends(get_images),
):
    """删除镜像：传 1 个走 DeleteImage，多个走 BatchDeleteImage。

    删除后会重新列一次校验，返回 `verified` / `still_present`；
    被容器引用的镜像默认拒绝删除（Docker 会拒绝，UGOS 却回 successful）。
    """
    try:
        return ok(await images.delete(ids, force=force))
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
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
    level: list[str] = Query(default=[], description="可重复；info/warning/critical（error 映射为 critical）"),
    module: str = "",
    keyword: str = "",
    since: int | None = Query(None, description="起始时间，Unix 秒"),
    until: int | None = Query(None, description="结束时间，Unix 秒"),
    order: str = Query("down", pattern="^(up|down)$"),
    order_param: str = Query("create_time", pattern="^(create_time|level|module|operator)$"),
    svc: LogService = Depends(get_logs),
):
    """系统日志：按级别 / 模块 / 关键词 / 时间范围过滤，支持分页与排序。

    实测：NAS 侧 `page` 从 0 开始（对外统一 1 起）；`size` 最大实测 500；
    `level` 只认 info/warning/critical，传 error 会 `8001 无效的输入参数`，这里已做别名映射。
    """
    try:
        return ok(await svc.query(page, size, level, module, keyword, since, until,
                                 order, order_param))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/logs/meta", dependencies=[Depends(require_auth)])
async def logs_meta(svc: LogService = Depends(get_logs)):
    """日志筛选项：可用级别 / 模块 / 操作者（来自 `v1/log/usersandmodules`）。"""
    return ok(await svc.meta())


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


@router.get("/services/health", dependencies=[Depends(require_auth)])
async def services_health(
    health: HealthService = Depends(get_health),
    alerts: AlertEngine = Depends(get_alerts),
):
    """关键服务健康：UGOS 应用服务 + 系统守护进程 + Docker 引擎。

    `taskmgr/services` 只有 UGOS 应用级服务（无 smbd / nfsd），
    系统守护进程从 `taskmgr/processes` 的进程名判断。
    """
    data = await health.services()
    data["baseline"] = alerts.service_baseline()
    return ok(data)


# ------------------------------------------------------------------ 备份 / 同步任务（只读）

@router.get("/backup/tasks", dependencies=[Depends(require_auth)])
async def backup_tasks(svc: BackupService = Depends(get_backup)):
    """同步与备份任务列表：名称 / 类型 / 状态 / 进度 / 上次与下次运行时间。

    本机没有配置任务，`schema_verified=false` 表示条目字段名只能按前端归一化逻辑推断；
    每个条目都带 `raw` 供客户端兜底。只读，不提供创建 / 删除 / 立即执行。
    """
    return ok(await svc.tasks())


@router.get("/backup/failures", dependencies=[Depends(require_auth)])
async def backup_failures(svc: BackupService = Depends(get_backup)):
    """最近失败的备份任务 + NAS 侧备份告警横幅。"""
    return ok(await svc.failures())


@router.get("/backup/runs", dependencies=[Depends(require_auth)])
async def backup_runs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    svc: BackupService = Depends(get_backup),
):
    """备份执行历史（`v1/web/sync/log/task/list`）。本机无任务，故为空。"""
    return ok(await svc.runs(page, size))


# ------------------------------------------------------------------ 虚拟机（只读）

@router.get("/vms", dependencies=[Depends(require_auth)])
async def vms(svc: VmService = Depends(get_vms)):
    """KVM 虚拟机列表（只读）。

    先查 `desktop/module/list` 判断是否安装虚拟机应用；未安装时 `installed=false`
    并说明探测过的路径。VM 启停 / 删除 / 快照回滚一律不实现。
    """
    return ok(await svc.status())


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
    push: PushService = Depends(get_push),
):
    """手动标记 / 取消「用户主动停止」。

    取消标记时若还有该容器的告警，会补发 resolved 事件，避免 App 一直停在告警态。
    """
    events = alerts.suppress(name, enabled)
    pushed = 0
    for ev in events:
        pushed += await push.send_alert(ev)
    return ok(
        {
            "container": name,
            "suppressed": enabled,
            "alert_events": [e.to_dict() for e in events],
            "pushed": pushed,
        }
    )


@router.post("/alerts/poll", dependencies=[Depends(require_auth)])
async def poll_now(alerts: AlertEngine = Depends(get_alerts), nas: NasService = Depends(get_nas),
                   push: PushService = Depends(get_push),
                   health: HealthService = Depends(get_health),
                   sessions: SessionService = Depends(get_sessions),
                   backup: BackupService = Depends(get_backup)):
    """手动触发一次巡检，便于联调。"""
    snapshot = await gather_snapshot(nas, health, sessions, backup)
    events = await alerts.evaluate(snapshot)
    pushed = 0
    for ev in events:
        pushed += await push.send_alert(ev)
    return ok(
        {
            "events": [e.to_dict() for e in events],
            "summary": alerts.summary(),
            "pushed": pushed,
            "degraded": snapshot.get("degraded") or [],
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
                {"key": "container_down", "level": "warning", "threshold": "连续 2 轮非运行",
                 "note": "仅对未建立期望基线的容器生效，与 container_expected_down 互斥"},
                {"key": "container_restart_loop", "level": "warning",
                 "threshold": f"{s.alert_container_restart_count} 次 / {s.alert_container_restart_window}s"},
                {"key": "nic_speed", "level": "warning",
                 "threshold": s.alert_nic_min_mbps or "低于历史峰值", "unit": "Mbps"},
                # --- 第三批 ---
                {"key": "container_expected_down", "level": "warning",
                 "threshold": f"连续 {s.alert_container_expected_polls} 轮 running 后转为停止",
                 "available": True,
                 "note": "命中 container_restart_loop 时不再发本条"},
                {"key": "disk_wake_freq", "level": "warning",
                 "threshold": s.alert_disk_wake_count,
                 "window_seconds": s.alert_disk_wake_window,
                 "unit": "次/窗口", "available": True,
                 "note": "计数由巡检采样累计，采样间隔内的短时唤醒会漏计"},
                {"key": "service_down", "level": "warning",
                 "threshold": f"连续 {s.alert_service_expected_polls} 轮存在后消失",
                 "available": True,
                 "note": "ALERT_REQUIRED_SERVICES 里点名的进程缺失会直接 critical"},
                {"key": "volume_inode", "level": "warning",
                 "threshold": s.alert_volume_inode,
                 "critical_at": s.alert_volume_inode_critical,
                 "unit": "%", "available": False,
                 "reason": "UGOS 存储接口不返回 inode 计数（volume/list、volume/describe、"
                           "volume/setting、storage/overview 均已验证），规则逻辑保留但不会触发"},
                {"key": "nic_errors", "level": "warning",
                 "threshold": s.alert_nic_error_delta,
                 "window_seconds": s.alert_nic_error_window,
                 "available": False,
                 "reason": "UGOS 未暴露网口错误包/丢弃包计数，规则逻辑保留但不会触发"},
                # --- 第四批 ---
                {"key": "login_bruteforce", "level": "warning",
                 "threshold": s.alert_login_fail_count,
                 "window_seconds": s.alert_login_fail_window,
                 "unit": "次/IP", "available": True,
                 "critical_at": s.alert_login_fail_count * 2,
                 "note": "数据源 v1/log/query?module=login（不看 level=info 的成功记录）；"
                         "NAS 自带 faillock 更严格，建议告警阈值不高于它"},
                {"key": "backup_task_failed", "level": "warning",
                 "threshold": None, "available": True,
                 "note": "任务状态位=失败即告警；本机未配置任务，用合成数据验证状态机"},
                {"key": "backup_task_stale", "level": "warning",
                 "threshold": s.alert_backup_stale_hours, "unit": "小时",
                 "available": True,
                 "note": "优先用上次运行时间；取不到时退回「状态未开始/等待中但计划时间已过」；"
                         "两条证据都没有则静默跳过"},
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


# ------------------------------------------------------------------ 文件管理

@router.get("/files/roots", dependencies=[Depends(require_auth)])
async def files_roots(files: FileService = Depends(get_files)):
    """可浏览的根分类（收藏/个人/共享/用户/设备/标签…）+ 卷列表。

    分类来自 NAS 的 `root_list`，只返回本机真实存在的那几个数字。
    """
    return ok(await files.roots())


@router.get("/files/list", dependencies=[Depends(require_auth)])
async def files_list(
    path: str | None = Query(None, description="/volume1/xxx 形式；与 root_type 二选一"),
    root_type: int | None = Query(None, description="根分类编号：2 个人 / 3 共享 / 4 用户 …"),
    page: int = Query(1, ge=1),
    limit: int = Query(200, ge=1, le=1000),
    sort: str = Query("name", description="name / size / type / mtime / ctime / atime / dtime"),
    reverse: bool = Query(False),
    files: FileService = Depends(get_files),
):
    """列目录。

    根级必须给 `root_type`（数字），子目录必须给 `path`；
    直接传卷路径（如 `/volume1`）会被 NAS 判为越界（`1301 不允许访问`），属正常行为。
    """
    try:
        return ok(await files.list_dir(path=path, root_type=root_type, page=page,
                                       limit=limit, sort=sort, reverse=reverse))
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc


@router.get("/files/download", dependencies=[Depends(require_auth)])
async def files_download(
    path: str = Query(..., description="要下载的文件绝对路径"),
    nas: NasService = Depends(get_nas),
    files: FileService = Depends(get_files),
):
    """流式下载单个文件。

    NAS 侧是三步链路（建任务 → 换 dl_token → 取字节），这里把三步包成一次请求，
    令牌不外泄，字节直接透传（不落盘、不进内存）。
    """
    try:
        info = await files.download_token(path)
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc

    name = posixpath.basename(info["path"]) or "download"
    # 中文文件名按 RFC 5987 编码，同时给 ASCII 兜底，避免个别客户端乱码
    ascii_name = name.encode("ascii", "ignore").decode() or "download"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"

    # 必须在返回 StreamingResponse **之前**确认上游可用：
    # 一旦响应头发出去，再抛异常只会让客户端读到半截就断连（incomplete chunked read）。
    ctx = nas.client.open_stream("v2/filemgr/downloadFile", {"token": info["token"]})
    try:
        resp = await ctx.__aenter__()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"连接 NAS 下载端点失败：{exc}"
        ) from exc

    if resp.status_code != 200:
        body = await resp.aread()
        await ctx.__aexit__(None, None, None)
        log.warning("下载失败 %s HTTP %s %r", info["path"], resp.status_code, body[:200])
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"NAS 下载返回 {resp.status_code}：{body[:120].decode('utf-8', 'replace')}",
        )

    async def pump():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await ctx.__aexit__(None, None, None)

    headers = {
        "Content-Disposition": disposition,
        "X-Nas-Path": quote(info["path"]),
    }
    length = resp.headers.get("content-length")
    if length and length.isdigit():
        headers["Content-Length"] = length

    return StreamingResponse(pump(), media_type="application/octet-stream", headers=headers)


@router.post("/files/folder", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_auth)])
async def files_create_folder(
    parent: str = Body(..., embed=True, description="父目录绝对路径"),
    name: str = Body(..., embed=True, description="新目录名（不含 /）"),
    files: FileService = Depends(get_files),
):
    """新建目录。NAS 的 `createFolder` 要的是新目录完整路径，这里由 parent+name 拼。"""
    try:
        return ok(await files.create_folder(parent, name))
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc


@router.post("/files/rename", dependencies=[Depends(require_auth)])
async def files_rename(
    path: str = Body(..., embed=True),
    new_name: str = Body(..., embed=True),
    files: FileService = Depends(get_files),
):
    try:
        return ok(await files.rename(path, new_name))
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc


@router.delete("/files", dependencies=[Depends(require_auth)])
async def files_delete(
    paths: list[str] = Query(..., description="绝对路径，可重复"),
    forever: bool = Query(False, description="true=不进回收站，直接删"),
    files: FileService = Depends(get_files),
):
    """删除。

    注意载荷形状：NAS 的 `delPaths` 只认**字符串数组**，传条目对象数组会得到 `9999 操作失败!`。
    默认进回收站（`forever=false`），与网页端行为一致。
    """
    try:
        return ok(await files.delete(paths, forever=forever))
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc


@router.get("/files/space", dependencies=[Depends(require_auth)])
async def files_space(
    path: str = Query(..., description="任意可访问路径"),
    files: FileService = Depends(get_files),
):
    """该路径所在卷的剩余字节数。"""
    try:
        return ok(await files.free_space(path))
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc


@router.get("/files/thumbnail", dependencies=[Depends(require_auth)])
async def files_thumbnail(
    path: str = Query(..., description="图片文件路径"),
    width: int = Query(88, ge=16, le=1024),
    height: int = Query(88, ge=16, le=1024),
    file_type: int = Query(1, description="条目 type，图片实测是 1"),
    mtime: int = Query(0, description="条目 mtime，作为 NAS 侧缓存键"),
    ctime: int = Query(0),
    file_size: int = Query(0),
    files: FileService = Depends(get_files),
):
    """文件缩略图，直接返回图片二进制。

    注意：`width` / `height` 会被 NAS 忽略，实测无论传多大都回同一张
    112×96 JPEG。要原图请用 `/files/preview`。
    """
    try:
        data, content_type = await files.thumbnail(
            path, width=width, height=height, file_type=file_type,
            mtime=mtime, ctime=ctime, file_size=file_size,
        )
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/files/preview", dependencies=[Depends(require_auth)])
async def files_preview(
    path: str = Query(..., description="文件路径"),
    file_type: int = Query(1, description="条目 type，图片通常是 1"),
    mtime: int = Query(0),
    ctime: int = Query(0),
    file_size: int = Query(0),
    files: FileService = Depends(get_files),
):
    """原图字节，用于大图预览。

    走 `mediaPlayer` 通道，返回的就是源文件本体（不是缩略图）。
    注意没有做 Range 支持，视频拖动播放请改用下载链路。
    """
    try:
        data, content_type = await files.preview(
            path, file_type=file_type,
            mtime=mtime, ctime=ctime, file_size=file_size,
        )
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=600"},
    )


@router.get("/files/recycle-config", dependencies=[Depends(require_auth)])
async def files_recycle_config(files: FileService = Depends(get_files)):
    """回收站开关状态。

    删除是否可恢复由**条目自身的 `recycle_open`** 决定（见 `/files/list` 的响应），
    这个接口给整体提示用：`recoverable=false` 时所有删除都是永久删除。
    """
    return ok(await files.recycle_conf())


@router.post("/files/transfer", dependencies=[Depends(require_auth)])
async def files_transfer(
    src: list[str] = Body(..., embed=True, description="要复制/移动的绝对路径数组"),
    dst: str = Body(..., embed=True, description="目标目录绝对路径"),
    move: bool = Body(False, embed=True, description="true=移动，false=复制"),
    action_type: int = Body(
        3, embed=True, description="0=跳过 1=覆盖 3=另存改名（默认）4=逐项询问"
    ),
    files: FileService = Depends(get_files),
):
    """复制 / 移动，返回 `{task_id, dst, src, move}`。

    这是**异步任务**：小文件实测 0.7 秒完成，但没有公开的进度查询接口
    （`getDelTaskInfo` 查不到这类任务，回 `1368` 或 `9999`）。
    客户端判断完成只能轮询目标目录，别去找任务接口。
    """
    try:
        result = await files.copy_or_move(
            src, dst, move=move, action_type=action_type
        )
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc
    return ok(result)


@router.post("/files/compress", dependencies=[Depends(require_auth)])
async def files_compress(
    paths: list[str] = Body(..., embed=True, description="要打包的绝对路径数组"),
    name: str = Body(..., embed=True, description="包名，**不含**后缀"),
    ext: str = Body("zip", embed=True, description="压缩格式后缀"),
    out: str | None = Body(None, embed=True, description="输出目录，默认取第一个路径的父目录"),
    conflict_options: int = Body(0, embed=True),
    files: FileService = Depends(get_files),
):
    """压缩成压缩包。返回 `{task_id, name, ext, out, paths}`。

    实测是异步任务（返回 uuid），但几百字节的文件 0.6 秒就出包。
    没有进度查询接口，客户端轮询输出目录判断落地。
    """
    try:
        return ok(await files.compress(
            paths, name, ext=ext, out=out, conflict_options=conflict_options
        ))
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc


@router.post("/files/extract", dependencies=[Depends(require_auth)])
async def files_extract(
    path: str = Body(..., embed=True, description="压缩包绝对路径"),
    out: str | None = Body(None, embed=True, description="解压目标目录，默认同名目录"),
    password: str | None = Body(None, embed=True, description="加密压缩包的密码"),
    action_type: int = Body(
        0, embed=True, description="0=跳过同名 1=覆盖 3=另存改名"
    ),
    files: FileService = Depends(get_files),
):
    """解压。返回 `{task_id, out, path}`。

    只认 `zip/rar/7z/tar/gz/tgz/iso`。`out` 需要是已存在目录，
    不存在先新建（已存在会回 `1327`，忽略即可）。
    """
    try:
        return ok(await files.extract(
            path, out=out, password=password, action_type=action_type
        ))
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc


@router.post("/files/upload", dependencies=[Depends(require_auth)])
async def files_upload(
    request: Request,
    parent: str = Query(..., description="目标目录绝对路径"),
    name: str = Query(..., description="文件名，不含路径"),
    action_type: int = Query(
        3, description="0/1/2 静默覆盖同名文件；3 同名自动改名（默认，不覆盖）"
    ),
    files: FileService = Depends(get_files),
):
    """上传单个文件：请求体就是文件裸字节，**必须**带 `Content-Length`。

    分块续传的细节封在服务层：这里把请求体当流读，逐块转给 NAS，不落盘也不整文件驻留内存。
    """
    length = request.headers.get("content-length", "")
    if not length.isdigit():
        raise HTTPException(
            status.HTTP_411_LENGTH_REQUIRED,
            detail="上传必须带 Content-Length；不支持 chunked 请求体",
        )
    total = int(length)
    if total <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="文件内容为空")

    try:
        return ok(await files.upload(parent, name, total, request.stream(), action_type))
    except FileRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.msg) from exc
