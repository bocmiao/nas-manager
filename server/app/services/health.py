"""系统健康：网口详情、关键服务、存储健康汇总。

三块数据的实测来源：
- 网口：`v1/sysinfo/machine/common` → `hardware.net`（只含**已连通**的网口，字段 model/ip/mac/speed/duplex/mtu/mask）
  + `v1/taskmgr/stat/net`（含未连通网口，字段 series[{name,label,send_rate,recv_rate}]）
  → 两个来源取并集，`eth1` 这种没插网的也能列出来
- 服务：`v1/taskmgr/services` 只有 **UGOS 应用级** 28 项（没有 smbd / nfsd），
  系统守护进程要从 `v1/taskmgr/processes` 的 339 条进程里找
- 存储：池级 `data_scrub`（校验）与 `sys_sync_progress`（重建/同步）、卷级容量；
  **inode 拿不到**，见 `docs/api-verified.md` 踩坑
"""
from __future__ import annotations

import logging
import time

from ..config import Settings
from .nas import NasService

log = logging.getLogger("health")

# 关注列表：出现在进程表里才算「在跑」的系统守护进程。
# 这里只是「观察清单」，是否告警由告警引擎的基线逻辑决定，避免对未启用的服务（如 NFS）误报。
SYSTEM_WATCH = (
    ("dockerd", "Docker 引擎守护进程"),
    ("containerd", "容器运行时"),
    ("smbd", "SMB / Samba 文件共享"),
    ("smbd-notifyd", "SMB 变更通知"),
    ("nfsd", "NFS 服务"),
    ("rpc.mountd", "NFS 挂载守护"),
    ("rpc.statd", "NFS 状态守护"),
    ("wsdd", "WS-Discovery（Windows 网络发现）"),
    ("sshd", "SSH 远程登录"),
    ("nginx", "内置 Web / 反向代理"),
    ("avahi-daemon", "mDNS 服务发现"),
)

# UGOS 应用服务里重点关注的（id → 说明），其余照常列出
UGOS_WATCH = {
    "docker_serv": "Docker 应用",
    "storage_serv": "存储管理",
    "filemgr_serv": "文件管理",
    "ctl_serv": "控制面板",
    "snapshot_serv": "快照",
}


class HealthService:
    def __init__(self, nas: NasService, settings: Settings) -> None:
        self._nas = nas
        self._s = settings

    # ---------------------------------------------------------------- 网口

    async def network(self, history=None) -> dict:
        system = await self._nas.system()
        hw = {n.get("name"): n for n in (system.get("nics") or []) if n.get("name")}

        rates: dict[str, dict] = {}
        try:
            raw = await self._nas.client.get("v1/taskmgr/stat/net") or {}
            for row in raw.get("series") or []:
                name = row.get("name")
                if name and name != "overview":
                    rates[name] = row
        except Exception as exc:  # noqa: BLE001 - 速率拿不到不影响静态信息
            log.warning("读取网口速率失败：%s", exc)

        names = sorted(set(hw) | set(rates))
        ports = []
        for name in names:
            base = hw.get(name) or {}
            rate = rates.get(name) or {}
            connected = bool(base.get("ip")) if base else None
            ports.append(
                {
                    "name": name,
                    "label": rate.get("label") or None,
                    "connected": connected,
                    "ip": base.get("ip"),
                    "mask": base.get("mask"),
                    "mac": base.get("mac"),
                    "speed_mbps": base.get("speed_mbps"),
                    "duplex": base.get("duplex"),
                    "mtu": base.get("mtu"),
                    "rx_bps": rate.get("recv_rate"),
                    "tx_bps": rate.get("send_rate"),
                    # UGOS 未暴露错误/丢弃计数：不编造，字段留空并置 supported=false
                    "rx_errors": None,
                    "tx_errors": None,
                    "rx_dropped": None,
                    "tx_dropped": None,
                    "counters_supported": False,
                    "traffic_total": None,
                }
            )

        out = {
            "total": len(ports),
            "connected": sum(1 for p in ports if p["connected"]),
            "ports": ports,
            "counters": {
                "supported": False,
                "reason": "UGOS 未暴露网口错误包/丢弃包计数与累计流量；"
                          "v1/sysinfo/machine/common 的 hardware.net 只有 "
                          "model/ip/mac/speed/duplex/mtu/mask，v1/taskmgr/stat/net 只有瞬时速率",
                "probed": [
                    "v1/network/interface/list", "v1/network/list", "v1/network/info",
                    "v1/network/nic/list", "v1/network/stat", "v1/network/traffic",
                    "v1/sysinfo/network", "v1/sysinfo/machine/net", "v1/netif/list",
                ],
            },
            "collected_at": int(time.time()),
        }
        if history is not None:
            out["traffic_estimate"] = history.estimate_net_bytes()
        return out

    # ---------------------------------------------------------------- 服务

    async def services(self) -> dict:
        ugos = await self._nas.services()
        procs = await self._nas.process_names()
        try:
            engine = await self._nas.docker_engine()
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 Docker 引擎状态失败：%s", exc)
            engine = {"status": "unknown", "healthy": None}

        required = [x.strip() for x in (self._s.alert_required_services or "").split(",") if x.strip()]

        # 关注清单 = 内置清单 ∪ 显式要求常驻的进程（后者可能不在内置清单里）
        watch = list(SYSTEM_WATCH)
        known = {name for name, _ in SYSTEM_WATCH}
        for name in required:
            if name not in known:
                watch.append((name, f"{name}（ALERT_REQUIRED_SERVICES 指定）"))

        system = []
        for name, label in watch:
            count = procs.get(name, 0)
            system.append(
                {
                    "name": name,
                    "label": label,
                    "present": count > 0,
                    "process_count": count,
                    "required": name in required,
                }
            )

        # 进程表偶尔会返回空：此时 present 全 False，若照常判定会一次性把
        # 11 个守护进程都报成「未运行」。如实标记不可信，告警引擎据此跳过。
        procs_total = sum(procs.values())
        reliable = procs_total > 0

        return {
            "ugos_services": {
                "total": ugos.get("total"),
                "items": [
                    {
                        **item,
                        "watched": item.get("id") in UGOS_WATCH,
                        "label": item.get("name"),
                    }
                    for item in ugos.get("items") or []
                ],
            },
            "system_processes": {
                "total": procs_total,
                "reliable": reliable,
                "watched": system,
                "missing": [x["name"] for x in system if not x["present"]] if reliable else [],
            },
            "docker_engine": engine,
            "required": {
                "configured": required,
                "missing": [n for n in required if procs.get(n, 0) == 0] if reliable else [],
                "source": "ALERT_REQUIRED_SERVICES",
            },
            "note": "taskmgr/services 只列 UGOS 应用级服务（本机 28 项，无 smbd/nfsd）；"
                    "系统守护进程取自 taskmgr/processes 的进程名。",
            "collected_at": int(time.time()),
        }

    # ---------------------------------------------------------------- 存储健康

    async def storage(self) -> dict:
        storage = await self._nas.storage()
        pools = storage.get("pools") or []
        volumes = storage.get("volumes") or []

        issues: list[dict] = []
        for p in pools:
            if not p.get("healthy", True):
                issues.append({"level": "critical", "scope": "pool",
                               "target": p.get("name"), "issue": "存储池状态异常"})
            if (p.get("sync") or {}).get("progress"):
                issues.append({"level": "info", "scope": "pool", "target": p.get("name"),
                               "issue": f"RAID 同步/重建中 {p['sync']['progress']}%"})
            if (p.get("scrub") or {}).get("running"):
                issues.append({"level": "info", "scope": "pool", "target": p.get("name"),
                               "issue": "数据校验进行中"})
            if p.get("no_redundancy"):
                issues.append({"level": "info", "scope": "pool", "target": p.get("name"),
                               "issue": f"{str(p.get('level')).upper()} 无冗余"})
        for v in volumes:
            if (v.get("used_percent") or 0) >= self._s.alert_volume_used:
                issues.append({"level": "warning", "scope": "volume", "target": v.get("name"),
                               "issue": f"空间已用 {v.get('used_percent')}%"})

        return {
            "pools": [
                {
                    "name": p.get("name"),
                    "label": p.get("label"),
                    "level": p.get("level"),
                    "status": p.get("status"),
                    "healthy": p.get("healthy"),
                    "redundant": p.get("redundant"),
                    "sync": p.get("sync") or {},
                    "scrub": p.get("scrub") or {},
                    "trim": p.get("trim") or {},
                    "spare": p.get("spare"),
                    "volumes": p.get("volumes") or [],
                }
                for p in pools
            ],
            "volumes": [
                {
                    "name": v.get("name"),
                    "label": v.get("label"),
                    "filesystem": v.get("filesystem"),
                    "mount": v.get("mount"),
                    "pool": v.get("pool"),
                    "total": v.get("total"),
                    "used": v.get("used"),
                    "available": v.get("available"),
                    "used_percent": v.get("used_percent"),
                    "health": v.get("health"),
                    "lazy_init": v.get("lazy_init") or {},
                    "balance": v.get("balance") or {},
                    "inode": {
                        "supported": False,
                        "used_percent": None,
                        "total": None,
                        "free": None,
                        "reason": "UGOS 存储接口不返回 inode 计数："
                                  "volume/list、volume/describe(POST device)、volume/setting、"
                                  "storage/overview 均无 inode 字段，中间层不自造数据",
                    },
                }
                for v in volumes
            ],
            "issues": issues,
            "level": (
                "critical" if any(i["level"] == "critical" for i in issues)
                else "warning" if any(i["level"] == "warning" for i in issues)
                else "ok"
            ),
            "collected_at": int(time.time()),
        }
