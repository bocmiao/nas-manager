"""UGOS 数据访问与归一化。

对上层只暴露稳定的数据结构，屏蔽 UGOS 私有接口的字段差异与版本漂移。
所有数据经 TTL 缓存，避免高频轮询打爆 NAS。
"""
from __future__ import annotations

import logging
from typing import Any

from ..cache import TTLCache
from ..config import Settings
from ..ugos.client import UgosClient

log = logging.getLogger("nas")

# 具备冗余能力的 RAID 级别
REDUNDANT_LEVELS = frozenset({"raid1", "raid5", "raid6", "raid10", "raidz1", "raidz2", "raidz3"})
# 无冗余级别
NO_REDUNDANCY_LEVELS = frozenset({"raid0", "basic", "jbod", "single"})

CONTAINER_ACTIONS = {
    "start": "StartContainer",
    "stop": "StopContainer",
    "restart": "RestartContainer",
}


def _first(seq: Any) -> dict:
    if isinstance(seq, list) and seq:
        return seq[0] if isinstance(seq[0], dict) else {}
    if isinstance(seq, dict):
        return seq
    return {}


def _result(data: Any) -> list[dict]:
    if isinstance(data, dict):
        for key in ("result", "list", "items"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


class NasService:
    def __init__(self, client: UgosClient, cache: TTLCache, settings: Settings) -> None:
        self._c = client
        self._cache = cache
        self._s = settings

    @property
    def has_session(self) -> bool:
        return self._c.has_session

    # ------------------------------------------------------------------ 系统

    async def system(self) -> dict:
        return await self._cache.get_or_set("system", self._s.ttl_system, self._fetch_system)

    async def _fetch_system(self) -> dict:
        raw = await self._c.get("v1/sysinfo/machine/common") or {}
        common = raw.get("common") or {}
        hw = raw.get("hardware") or {}
        cpu = _first(hw.get("cpu"))
        mems = hw.get("mem") or []
        mem_total = sum(int(m.get("size") or 0) for m in mems if isinstance(m, dict))

        return {
            "name": common.get("nas_name"),
            "model": common.get("model"),
            "serial": common.get("serial"),
            "firmware": common.get("system_version"),
            "product_series": common.get("product_series"),
            "model_series": common.get("model_series"),
            "uptime_seconds": int(common.get("run_time") or 0),
            "boot_at": common.get("last_turn_on_timestamp"),
            "cpu": {
                "model": cpu.get("model"),
                "cores": cpu.get("core"),
                "threads": cpu.get("thread"),
                "mhz": cpu.get("ghz"),
                "temperature": cpu.get("temperature"),
            },
            "memory": {
                "total": mem_total,
                "modules": [
                    {
                        "manufacturer": m.get("manufacturer"),
                        "model": m.get("model"),
                        "size": int(m.get("size") or 0),
                        "mhz": m.get("mhz"),
                        "ecc": bool(m.get("is_ecc")),
                    }
                    for m in mems
                    if isinstance(m, dict)
                ],
            },
            "nics": [
                {
                    "name": n.get("model"),
                    "ip": n.get("ip"),
                    "mac": n.get("mac"),
                    "speed_mbps": n.get("speed"),
                    "duplex": n.get("duplex"),
                    "mtu": n.get("mtu"),
                    "mask": n.get("mask"),
                }
                for n in (hw.get("net") or [])
                if isinstance(n, dict)
            ],
            "ups": hw.get("ups"),
            "usb": hw.get("usb") or [],
            "gpu": hw.get("gpu"),
        }

    # ------------------------------------------------------------------ 实时

    async def live(self) -> dict:
        return await self._cache.get_or_set("live", self._s.ttl_overview, self._fetch_live)

    async def _fetch_live(self) -> dict:
        raw = await self._c.get("v1/taskmgr/stat/overview") or {}
        cpu = _first(raw.get("cpu"))
        mem = _first(raw.get("mem"))
        net = _first(raw.get("net"))
        disk = _first(raw.get("disk"))
        vol = _first(raw.get("volume"))
        gpu = _first(raw.get("gpu"))
        cpu_fan = _first(raw.get("cpu_fan"))
        dev_fan = _first(raw.get("device_fan"))

        return {
            "ts": cpu.get("time") or mem.get("time"),
            "cpu": {"used_percent": cpu.get("used_percent"), "temp": cpu.get("temp")},
            "memory": {"used_percent": mem.get("used_percent")},
            "net": {
                "send_bps": net.get("send_rate") or 0,
                "recv_bps": net.get("recv_rate") or 0,
            },
            "disk_io": {
                "read_bps": disk.get("read_rate") or 0,
                "write_bps": disk.get("write_rate") or 0,
            },
            "volume_io": {
                "read_bps": vol.get("read_rate") or 0,
                "write_bps": vol.get("write_rate") or 0,
            },
            "gpu": {
                "used_percent": gpu.get("used_percent"),
                "temp": gpu.get("temp"),
                "name": gpu.get("gpu_name"),
            },
            "fans": {
                "cpu": {"speed": cpu_fan.get("speed"), "status": cpu_fan.get("status")},
                "device": {"speed": dev_fan.get("speed"), "status": dev_fan.get("status")},
            },
        }

    # ------------------------------------------------------------------ 存储

    async def storage(self) -> dict:
        return await self._cache.get_or_set("storage", self._s.ttl_storage, self._fetch_storage)

    async def _fetch_storage(self) -> dict:
        disks = _result(await self._c.get("v2/storage/disk/list"))
        pools = _result(await self._c.get("v1/storage/pool/list"))
        volumes = _result(await self._c.get("v1/storage/volume/list"))

        disk_out = [
            {
                "name": d.get("name"),
                "label": d.get("label"),
                "slot": d.get("slot"),
                "brand": d.get("brand"),
                "model": d.get("model"),
                "serial": d.get("serial"),
                "size": int(d.get("size") or 0),
                "interface": d.get("interface_type"),
                "temperature": d.get("temperature"),
                "power_on_hours": d.get("power_on_hours"),
                "status": d.get("status"),
                # 实测：status 0/1 均为正常（1 = 已激活在用），其余视为异常
                "healthy": d.get("status") in (0, 1),
                "used_for": d.get("used_for"),
                "support_smart": bool(d.get("is_support_smart")),
                "support_locate": bool(d.get("is_support_located")),
                "standby": bool(d.get("is_standby")),
            }
            for d in disks
        ]

        pool_out = []
        for p in pools:
            total = int(p.get("total") or 0)
            used = int(p.get("used") or 0)
            level = (p.get("level") or "").lower()
            pool_out.append(
                {
                    "name": p.get("name"),
                    "label": p.get("label"),
                    "level": level,
                    "status": p.get("status"),
                    "healthy": p.get("status") == 0,
                    "total": total,
                    "used": used,
                    "free": int(p.get("free") or 0),
                    "disk_count": p.get("total_disk_num"),
                    "redundant": level in REDUNDANT_LEVELS,
                    "no_redundancy": level in NO_REDUNDANCY_LEVELS,
                    "sync_progress": p.get("sys_sync_progress"),
                    "disks": [d.get("label") or d.get("name") for d in (p.get("disks") or [])],
                }
            )

        vol_out = []
        for v in volumes:
            total = int(v.get("total") or 0)
            used = int(v.get("used") or 0)
            vol_out.append(
                {
                    "name": v.get("name"),
                    "label": v.get("label"),
                    "filesystem": v.get("filesystem"),
                    "total": total,
                    "used": used,
                    "available": int(v.get("available") or 0),
                    "used_percent": round(used / total * 100, 1) if total else 0.0,
                    "mount": v.get("mntpath"),
                    "mounts": v.get("mntpaths") or [],
                    "pool": v.get("poolname"),
                    "health": v.get("health"),
                    "status": v.get("status"),
                    "is_home": bool(v.get("is_home")),
                }
            )

        return {"disks": disk_out, "pools": pool_out, "volumes": vol_out}

    # ------------------------------------------------------------------ 容器

    async def containers(self) -> list[dict]:
        return await self._cache.get_or_set(
            "containers", self._s.ttl_docker, self._fetch_containers
        )

    async def _fetch_containers(self) -> list[dict]:
        raw = await self._c.post(
            "v1/docker/container/ContainerListV2", {"PageNum": 1, "PageSize": 500}
        ) or {}
        return [
            {
                "name": c.get("name"),
                "id": c.get("containerId"),
                "image": c.get("imageName"),
                "version": c.get("version"),
                "project": c.get("projectName") or "",
                "status": c.get("status") or "unknown",
                "running": (c.get("status") or "") == "running",
                "created_at": c.get("createAt"),
                "application": c.get("application") or "",
            }
            for c in _result(raw)
        ]

    async def container_action(self, container_id: str, action: str) -> dict:
        endpoint = CONTAINER_ACTIONS.get(action)
        if not endpoint:
            raise ValueError(f"不支持的操作：{action}")
        if not container_id or len(container_id) < 8:
            raise ValueError("containerId 非法")
        data = await self._c.get(
            f"v1/docker/container/{endpoint}", {"containerId": container_id}
        )
        self._cache.invalidate("containers")
        return {"action": action, "container_id": container_id, "result": data}

    async def container_logs(self, container_id: str, tail: int = 200) -> dict:
        body = {"containerId": container_id, "ContainerId": container_id, "tail": tail, "Tail": tail}
        data = await self._c.post("v1/docker/container/ShowContainerLogs", body)
        return {"container_id": container_id, "data": data}

    async def docker_usage(self) -> dict:
        cpu = await self._c.get("v1/docker/container/ShowCpu")
        mem = await self._c.get("v1/docker/container/ShowMemory")
        return {"cpu": cpu, "memory": mem}

    # ------------------------------------------------------------------ 日志

    async def logs(self, page: int = 1, size: int = 50, level: str = "",
                   module: str = "", keyword: str = "") -> dict:
        params: dict[str, Any] = {"page": page, "size": size}
        if level:
            params["level"] = level
        if module:
            params["module"] = module
        if keyword:
            params["keyword"] = keyword
        raw = await self._c.get("v1/log/query", params) or {}
        items = raw.get("log_list") or []
        return {
            "page": raw.get("cur_page", page),
            "total": raw.get("total", len(items)),
            "items": [
                {
                    "id": it.get("log_id"),
                    "level": it.get("level"),
                    "module": it.get("module"),
                    "operator": it.get("operator"),
                    "content": it.get("content"),
                    "time": it.get("create_time"),
                }
                for it in items
                if isinstance(it, dict)
            ],
        }

    # ------------------------------------------------------------------ 能力探测

    async def capabilities(self) -> dict:
        return await self._cache.get_or_set("caps", 60.0, self._probe_capabilities)

    async def _probe_capabilities(self) -> dict:
        probes = {
            "system": "v1/sysinfo/machine/common",
            "monitor": "v1/taskmgr/stat/overview",
            "disks": "v2/storage/disk/list",
            "pools": "v1/storage/pool/list",
            "volumes": "v1/storage/volume/list",
            "logs": "v1/log/query",
            "firmware": "v1/firmware/status",
            "files": "v1/filemgr/getHomeFolderSet",
        }
        caps: dict[str, Any] = {}
        for name, path in probes.items():
            try:
                await self._c.get(path)
                caps[name] = True
            except Exception as exc:  # noqa: BLE001 - 探测失败即视为不支持
                log.warning("能力探测失败 %s: %s", name, exc)
                caps[name] = False
        try:
            await self._c.post(
                "v1/docker/container/ContainerListV2", {"PageNum": 1, "PageSize": 1}
            )
            caps["docker"] = True
        except Exception:  # noqa: BLE001
            caps["docker"] = False
        return caps
