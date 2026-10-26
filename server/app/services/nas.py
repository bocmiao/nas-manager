"""UGOS 数据访问与归一化。

对上层只暴露稳定的数据结构，屏蔽 UGOS 私有接口的字段差异与版本漂移。
所有数据经 TTL 缓存，避免高频轮询打爆 NAS。
"""
from __future__ import annotations

import logging
import time
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


def _pick(data: dict, keys: tuple[str, ...]):
    """按候选键名取第一个非空值，用于字段名未确认的结构（如 UPS）。"""
    for k in keys:
        val = data.get(k)
        if val is not None and val != "":
            return val
    return None


# SMART 属性 ID → 归一化字段。ID 取自 ATA 规范，实测本机 WD40EFPX 全部命中。
SMART_FIELDS = {
    1: "read_error_rate",
    5: "reallocated_sectors",
    7: "seek_error_rate",
    9: "power_on_hours",
    10: "spin_retry_count",
    12: "power_cycle_count",
    187: "reported_uncorrectable",
    194: "temperature",
    196: "reallocated_event_count",
    197: "pending_sectors",
    198: "offline_uncorrectable",
    199: "udma_crc_errors",
}

# 累计计数类属性：只有 raw > 0 才代表真有坏块/错误
SMART_COUNTER_FIELDS = (
    "reallocated_sectors",
    "pending_sectors",
    "offline_uncorrectable",
    "reported_uncorrectable",
    "reallocated_event_count",
    "udma_crc_errors",
)


def demux_docker_log(raw: str) -> str:
    """解 Docker 多路复用日志流。

    每帧结构：1 字节流类型 + 3 字节保留 + 4 字节大端「字节长度」+ 载荷。
    注意长度按 UTF-8 字节计，而响应体已被服务端解码成 str，
    因此要按字符累加其 UTF-8 字节数来对齐，不能直接用字符下标。
    解析失败时原样返回，避免把日志变成空串。
    """
    out: list[str] = []
    i, n = 0, len(raw)
    while i + 8 <= n:
        head = raw[i : i + 8]
        try:
            length = int.from_bytes(head[4:8].encode("latin-1"), "big")
        except UnicodeEncodeError:
            return raw
        if length <= 0 or length > n * 4:
            return raw
        j, consumed = i + 8, 0
        while j < n and consumed < length:
            consumed += len(raw[j].encode("utf-8"))
            j += 1
        if consumed != length:
            return raw
        out.append(raw[i + 8 : j])
        i = j
    if not out:
        return raw
    return "".join(out)


def _smart_overall(attrs: list[dict]) -> str:
    """属性 status：实测 1 = 正常。其余一律视为异常，原始码保留给上层判断。"""
    bad = [a for a in attrs if a.get("status") not in (1, None)]
    if not bad:
        return "ok"
    return "warning" if len(bad) < 3 else "failed"


class NasService:
    def __init__(self, client: UgosClient, cache: TTLCache, settings: Settings) -> None:
        self._c = client
        self._cache = cache
        self._s = settings

    @property
    def has_session(self) -> bool:
        return self._c.has_session

    @property
    def client(self) -> UgosClient:
        """暴露底层客户端，供镜像/快照等跨域模块复用同一会话。"""
        return self._c

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

    # ------------------------------------------------------------------ 固件 / UPS

    async def firmware(self) -> dict:
        """固件版本查询（只读）。

        实测：`firmware/status` 的 `current_version` 是字符串（1.19.1.0126），
        而 `firmware/version/is_new` 里同名字段是整数（119010126），不能混用。
        本中间层**不提供升级动作**，升级不可逆且 App 端只做提醒。
        """
        return await self._cache.get_or_set("firmware", self._s.ttl_system, self._fetch_firmware)

    async def _fetch_firmware(self) -> dict:
        status = await self._c.get("v1/firmware/status") or {}
        newest = await self._c.get("v1/firmware/version/is_new") or {}
        remote = status.get("remote_base") or {}

        # 版本号两种形态：status 里是 "1.20.0.0142"，is_new 里是 120000142
        latest = remote.get("versionName") or ""
        publish_num = newest.get("publish_version")
        has_update = bool(newest.get("has_version"))
        if not latest and publish_num:
            latest = str(publish_num)

        return {
            "current_version": status.get("current_version") or "",
            "latest_version": latest or None,
            "has_update": has_update,
            "beta": bool(remote.get("beta")),
            "size_bytes": int(remote.get("size") or remote.get("byteSize") or 0),
            "notes": remote.get("desc") or "",
            "checking": bool(status.get("running")),
            # 以下三个原始码含义未完全确认（本机 current_stat=2 / operate_type=5），原样透出
            "state": status.get("current_stat"),
            "operate_type": status.get("operate_type"),
            "request_failed_reason": status.get("request_failed_reason"),
            "last_check_at": status.get("prev_check_timestamp"),
            "version_numbers": {
                "current": newest.get("current_version"),
                "publish": publish_num,
            },
            "upgrade_supported": False,
            "note": "只读查询。固件升级不可逆，中间层不实现升级动作。",
            "collected_at": int(time.time()),
        }

    async def ups(self) -> dict:
        """UPS 状态。

        `sysinfo/machine/common` 的 `hardware.ups` 在未接 UPS 时恒为 `null`，
        已接时的字段名**本机无法验证**，因此按常见命名做多键兜底，
        并把原始对象整包透出，客户端可自行回退。
        """
        system = await self.system()
        raw = system.get("ups")
        if not isinstance(raw, dict) or not raw:
            return {
                "supported": False,
                "connected": False,
                "verified": True,
                "reason": "hardware.ups 为 null：未接入 UPS，或 UPS 未被 UGOS 识别",
                "raw": raw,
                "collected_at": int(time.time()),
            }

        return {
            "supported": True,
            "connected": True,
            "verified": False,
            "model": _pick(raw, ("model", "name", "product", "vendor", "manufacturer")),
            "status": _pick(raw, ("status", "state", "connect_status", "link_status")),
            "battery_percent": _pick(
                raw, ("battery_charge", "charge", "battery_percent", "capacity",
                      "battery_level", "percent", "battery")
            ),
            "runtime_seconds": _pick(
                raw, ("runtime", "battery_runtime", "remain_time", "left_time",
                      "run_time", "remaining_time")
            ),
            "on_battery": _pick(
                raw, ("on_battery", "onbattery", "battery_power", "using_battery",
                      "is_battery", "power_supply_fail", "ac_lost")
            ),
            "events": _pick(raw, ("events", "event_list", "switch_events", "last_event")),
            "raw": raw,
            "note": "字段名未经真机验证（本机 hardware.ups 为 null），normalized 值仅供参考",
            "collected_at": int(time.time()),
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
            scrub = p.get("data_scrub") or {}
            trim = p.get("trim") or {}
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
                    # 以下为存储健康汇总所需，均为原始字段 + 少量实测语义
                    "sync": {
                        # RAID 重建/同步进度（%）。本机 sys_sync_progress 恒 0，字段语义按名称推断
                        "progress": p.get("sys_sync_progress"),
                        "delayed": bool(p.get("is_sync_delay")),
                        "sync_time": p.get("sync_time"),
                    },
                    "scrub": {
                        # 实测：data_scrub.status == 1 表示正在校验（前端 isOrganizeNow）
                        "running": scrub.get("status") == 1,
                        "status": scrub.get("status"),
                        "progress": scrub.get("process"),
                        "need_time": scrub.get("need_time"),
                        "time_remaining": scrub.get("time_remaining"),
                        "last_time": scrub.get("last_time"),
                        "next_start_time": scrub.get("next_start_time"),
                        "scheduled": bool(scrub.get("is_cron")),
                        "schedule": scrub.get("scheduled") or {},
                        "supported": level in ("raid5", "raid6"),
                    },
                    "trim": {
                        "enabled": bool(trim.get("enable")),
                        "supported": bool(trim.get("is_support")),
                        "next_start_time": trim.get("next_start_time"),
                    },
                    "spare": p.get("spare"),
                    "volumes": [v.get("name") for v in (p.get("volumes") or []) if isinstance(v, dict)],
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
                    "space_alert_percent": v.get("space_alert_percent"),
                    "warn_flags": v.get("warn_flags"),
                    "lazy_init": v.get("lazy_init_info") or {},
                    "balance": v.get("balance_info") or {},
                    # 实测：UGOS 存储接口不返回 inode 计数，此处恒为 None，由上层标注不支持
                    "inode_used_percent": None,
                    "inode_total": None,
                    "inode_free": None,
                }
            )

        return {"disks": disk_out, "pools": pool_out, "volumes": vol_out}

    # ------------------------------------------------------------------ SMART

    async def resolve_disk(self, ref: str) -> dict:
        """把 sda / /dev/sda / 硬盘1 统一解析成磁盘条目。

        SMART 与测速接口的 `disk` 参数实测必须传 dev_name（形如 /dev/sda），
        传裸盘名 sda 会返回 code 5001「硬盘不存在」。
        """
        key = (ref or "").strip().lower()
        if not key:
            raise ValueError("磁盘标识不能为空")
        disks = (await self.storage()).get("disks") or []
        for d in disks:
            dev = f"/dev/{d.get('name')}".lower()
            names = {
                str(d.get("name") or "").lower(),
                dev,
                str(d.get("label") or "").lower(),
                str(d.get("serial") or "").lower(),
            }
            if key in names:
                return d
        raise ValueError(f"未找到磁盘：{ref}")

    async def disk_smart(self, ref: str) -> dict:
        disk = await self.resolve_disk(ref)
        dev = f"/dev/{disk.get('name')}"

        async def _fetch() -> dict:
            if not disk.get("support_smart"):
                return {"supported": False, "reason": "该磁盘不支持 SMART", "disk": disk}
            raw = await self._c.get("v1/storage/disk/smart/info", {"disk": dev}) or {}
            report = [a for a in (raw.get("report") or []) if isinstance(a, dict)]

            attrs = [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "label": a.get("label"),
                    "value": a.get("value"),
                    "worst": a.get("worst"),
                    "threshold": a.get("thresh"),
                    "raw": a.get("raw"),
                    "raw_string": a.get("raw_string"),
                    "status": a.get("status"),
                }
                for a in report
            ]
            # 关键项：按 ATA 属性 ID 取 raw 值
            key_items: dict[str, Any] = {}
            for a in report:
                field = SMART_FIELDS.get(a.get("id"))
                if field:
                    key_items[field] = a.get("raw")

            overall = _smart_overall(attrs)
            last = raw.get("last") or {}
            return {
                "supported": True,
                "disk": {
                    "name": disk.get("name"),
                    "dev": dev,
                    "label": disk.get("label"),
                    "model": disk.get("model"),
                    "serial": disk.get("serial"),
                    "slot": disk.get("slot"),
                    "size": disk.get("size"),
                },
                "health": overall,
                "key_items": key_items,
                "counters": {k: key_items.get(k) for k in SMART_COUNTER_FIELDS},
                "self_test": {
                    "running": bool(raw.get("is_testing")),
                    "type": raw.get("running_smart_type"),
                    "progress": raw.get("progress"),
                    "remain_seconds": raw.get("remain_time"),
                    "short_test_seconds": raw.get("short_test_cost"),
                    "long_test_seconds": raw.get("long_test_cost"),
                    "last_status": last.get("status"),
                    "last_at": last.get("last_time"),
                    "next_at": last.get("next_time"),
                },
                "attributes": attrs,
                "collected_at": int(time.time()),
            }

        return await self._cache.get_or_set(f"smart:{dev}", self._s.ttl_smart, _fetch)

    async def disk_state(self, ref: str) -> dict:
        disk = await self.resolve_disk(ref)
        raw = await self._c.get(
            "v1/storage/disk/runningState", {"disk": f"/dev/{disk.get('name')}"}
        ) or {}
        return {
            "disk": disk.get("name"),
            "status": raw.get("status"),
            "temperature": raw.get("temperature"),
            "power_start_at": raw.get("power_start_time"),
            "reconnect_count": raw.get("reconnect_count"),
            "rescan_count": raw.get("rescan_count"),
            "reallocated_sector_count": raw.get("reallocated_sector_count"),
            "spin_retry_count": raw.get("spin_retry_count"),
            "nvme_percentage_used": raw.get("nvme_percentage_used"),
            "badblocks_by_month": raw.get("key_datas") or [],
        }

    async def all_smart(self) -> tuple[list[dict], list[str]]:
        """巡检用：批量取所有支持 SMART 的磁盘，返回 `(明细, 本轮跳过的盘名)`。

        **休眠中的盘直接跳过**：读 SMART 需要盘转起来，对 `is_standby=true` 的盘取
        SMART 相当于由中间层自己把盘唤醒，会让用户的休眠策略彻底失效。
        代价是休眠期间的 SMART 数据停在旧值（温度等），恢复工作后自动更新。

        跳过的盘（休眠 + 单盘读取失败）单独返回：调用方知道「本轮没有数据」，
        才能做到既不新增也不恢复告警。
        """
        disks = (await self.storage()).get("disks") or []
        out: list[dict] = []
        skipped: list[str] = []
        for d in disks:
            name = d.get("name") or ""
            if not d.get("support_smart"):
                continue
            if d.get("standby"):
                log.debug("%s 处于休眠，跳过 SMART 以免唤醒", name)
                skipped.append(name)
                continue
            try:
                out.append(await self.disk_smart(name))
            except Exception as exc:  # noqa: BLE001 - 单盘失败降级
                log.warning("读取 %s SMART 失败：%s", name, exc)
                skipped.append(name)
        return out, skipped

    # ------------------------------------------------------------------ 容器

    async def containers(self) -> list[dict]:
        return await self._cache.get_or_set(
            "containers", self._s.ttl_docker, self._fetch_containers
        )

    async def _fetch_containers(self) -> list[dict]:
        raw = await self._c.post(
            "v1/docker/container/ContainerListV2", {"PageNum": 1, "PageSize": 500}
        ) or {}
        # 明细接口提供 restarting / cpuUsage / memUsage / 流量，按 id 合并
        detail: dict[str, dict] = {}
        try:
            det = await self._c.post(
                "v1/docker/container/ShowContainerDetailListV2", {"PageNum": 1, "PageSize": 500}
            ) or {}
            for d in det.get("result") or []:
                if isinstance(d, dict) and d.get("containerId"):
                    detail[d["containerId"]] = d
        except Exception as exc:  # noqa: BLE001 - 明细不可用不影响列表
            log.warning("容器明细获取失败：%s", exc)

        out = []
        for c in _result(raw):
            cid = c.get("containerId") or ""
            d = detail.get(cid) or {}
            status = d.get("status") or c.get("status") or "unknown"
            out.append(
                {
                    "name": c.get("name"),
                    "id": cid,
                    "id_short": cid[:12],
                    "image": c.get("imageName"),
                    "version": c.get("version"),
                    "project": c.get("projectName") or "",
                    "status": status,
                    "running": status == "running",
                    "restarting": bool(d.get("restarting")),
                    "cpu_percent": d.get("cpuUsage"),
                    "mem_bytes": d.get("memUsage"),
                    "net_tx_bytes": d.get("upstreamTraffic"),
                    "net_rx_bytes": d.get("downstreamTraffic"),
                    "ports": d.get("portMapping") or [],
                    "created_at": c.get("createAt"),
                    "application": c.get("application") or "",
                }
            )
        return out

    async def container_action(self, container_id: str, action: str) -> dict:
        endpoint = CONTAINER_ACTIONS.get(action)
        if not endpoint:
            raise ValueError(f"不支持的操作：{action}")
        if not container_id or len(container_id) < 8:
            raise ValueError("containerId 非法")
        # 反查容器名要给告警引擎用：用户在 App 里停的容器不该被报成「非用户主动停止」。
        # 必须在动作前查（动作后缓存已被失效，会重新拉一次且已是停止状态）。
        name = None
        try:
            name = next(
                (c.get("name") for c in await self.containers() if c.get("id") == container_id),
                None,
            )
        except Exception as exc:  # noqa: BLE001 - 反查失败不影响动作本身
            log.warning("反查容器名失败：%s", exc)

        data = await self._c.get(
            f"v1/docker/container/{endpoint}", {"containerId": container_id}
        )
        self._cache.invalidate("containers")
        return {
            "action": action,
            "container_id": container_id,
            "container_name": name,
            "result": data,
        }

    async def container_logs(self, container_id: str, tail: int = 200,
                             keyword: str = "", stream: str = "") -> dict:
        """容器日志。

        实测：请求体只用 containerId / tail 两个字段即可（两者大小写都能过校验），
        但服务端**忽略行数参数**、始终返回完整日志，因此 tail 在本地截断。
        """
        if not container_id or len(container_id) < 8:
            raise ValueError("containerId 非法")
        raw = await self._c.post(
            "v1/docker/container/ShowContainerLogs",
            {"containerId": container_id, "tail": tail},
        ) or {}
        body = raw.get("result") or {}
        message = body.get("message") or ""
        text = demux_docker_log(message)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if keyword:
            kw = keyword.lower()
            lines = [ln for ln in lines if kw in ln.lower()]
        if stream in ("stdout", "stderr"):
            # 解帧后已丢失流类型，这里只做兜底提示，不假装能区分
            pass
        truncated = len(lines) > tail
        keep = lines[-tail:]
        return {
            "container_id": container_id,
            "requested_tail": tail,
            "total_lines": len(lines),
            "returned_lines": len(keep),
            "truncated": truncated,
            "keyword": keyword or None,
            "lines": keep,
        }

    async def container_detail(self, container_id: str) -> dict:
        """容器详情：环境变量、端口映射、卷、网络、资源限制。

        实测契约（`GET v1/docker/container/GetContainerById`）：
        - 环境变量数组的字段名是 `variable` + **`price`**（UGOS 把 value 拼错成 price）
        - 端口映射 `portMapping[{nasPort, containerPort, portType}]`
        - **没有 compose 内容**：docker 应用 87 个端点里没有任何 compose/project/yaml 读取接口，
          compose 文件只存在 NAS 磁盘上（`/volume1/@appstore/...`），需走文件管理接口才能读到
        """
        if not container_id or len(container_id) < 8:
            raise ValueError("containerId 非法")
        raw = await self._c.get(
            "v1/docker/container/GetContainerById", {"containerId": container_id}
        ) or {}

        env = []
        for e in raw.get("environmentVariables") or []:
            if not isinstance(e, dict):
                continue
            env.append(
                {
                    "key": e.get("variable") or e.get("name"),
                    "value": e.get("price") if "price" in e else e.get("value"),
                }
            )

        ports = [
            {
                "nas_port": p.get("nasPort"),
                "container_port": p.get("containerPort"),
                "protocol": p.get("portType"),
            }
            for p in (raw.get("portMapping") or [])
            if isinstance(p, dict)
        ]

        volumes = [
            {
                "nas_path": v.get("nasDirectory"),
                "container_path": v.get("containerDirectory"),
                "mode": v.get("containerPermission"),
            }
            for v in (raw.get("volumes") or [])
            if isinstance(v, dict)
        ]

        return {
            "id": container_id,
            "id_short": container_id[:12],
            "name": raw.get("containerName"),
            "image": raw.get("imageName"),
            "tag": raw.get("tag") or raw.get("imageVersion"),
            "image_id": raw.get("imageId"),
            "project": raw.get("projectName") or "",
            "running": bool(raw.get("runContainer")),
            "network": {
                "mode": raw.get("networkMode"),
                "subnets": raw.get("subnetSettings") or [],
                "nas_ips": raw.get("nasIPs") or [],
            },
            "limits": {
                "no_restrictions": bool(raw.get("noRestrictions")),
                "cpu_limit": raw.get("cpuLimit"),
                "mem_limit": raw.get("memLimit"),
            },
            "privileged": bool(raw.get("privilegedMode")),
            "hardware_acceleration": bool(raw.get("hardwareAcceleration")),
            "environment": env,
            "ports": ports,
            "volumes": volumes,
            "run_command": raw.get("containerRunCommand"),
            # compose 内容拿不到，明确告知客户端不要期待这个字段
            "compose": {
                "available": False,
                "supported": False,
                "reason": "UGOS Docker 应用未暴露 compose 读取端点；"
                          "compose 文件仅存于 NAS 磁盘，需用文件管理接口读取",
            },
            "collected_at": int(time.time()),
        }

    async def docker_usage(self) -> dict:
        """Docker 资源占用。

        `ShowCpu` / `ShowMemory` 实测是**宿主机级**（传 containerId 也不生效），
        真正的按容器数据在 `docker/view/ObtainOverviewInfo` 的 overviewContainers。
        """
        raw = await self._c.get("v1/docker/view/ObtainOverviewInfo") or {}
        items = []
        for c in raw.get("overviewContainers") or []:
            if not isinstance(c, dict):
                continue
            cid = c.get("containerId") or ""
            items.append(
                {
                    "name": c.get("containerName"),
                    "id": cid,
                    "id_short": cid[:12],
                    "cpu_percent": c.get("cpuUsagePercentage"),
                    "mem_bytes": c.get("memoryUsagePercentage"),
                    "mem_percent": (
                        round(c.get("memoryUsagePercentage") / raw["totalMemory"] * 100, 2)
                        if raw.get("totalMemory") and c.get("memoryUsagePercentage") is not None
                        else None
                    ),
                    "net_tx_bytes": c.get("upstreamTraffic"),
                    "net_rx_bytes": c.get("downstreamTraffic"),
                }
            )
        items.sort(key=lambda x: (x.get("cpu_percent") or 0), reverse=True)
        return {
            "engine": {
                "containers": raw.get("containerCount"),
                "running": raw.get("runContainerCount"),
                "images": raw.get("imageCount"),
                "status": raw.get("status"),
            },
            "host": {
                "cpu_percent": raw.get("cpuUsed"),
                "mem_used": raw.get("memoryUsed"),
                "mem_total": raw.get("totalMemory"),
            },
            "docker": {
                "cpu_percent": raw.get("containerCpuUsed"),
                "mem_used": raw.get("containerMemory"),
                "mem_total": raw.get("containerTotalMemory"),
            },
            "containers": items,
            "collected_at": int(time.time()),
            "note": "mem_percent 为按容器内存 / 宿主机总内存折算；net_* 为累计字节数，非速率",
        }

    async def processes(self, limit: int = 50, sort: str = "cpu") -> dict:
        raw = await self._c.get("v1/taskmgr/processes") or {}
        items = [p for p in (raw.get("list") or []) if isinstance(p, dict)]
        field = "mem_used" if sort == "mem" else "cpu_used_percent"
        items.sort(key=lambda p: (p.get("consume") or {}).get(field) or 0, reverse=True)

        def _norm(p: dict) -> dict:
            c = p.get("consume") or {}
            return {
                "name": p.get("name"),
                "pid": p.get("pid"),
                "status": p.get("process_status"),
                "status_text": p.get("status"),
                "operable": bool(p.get("can_be_operated")),
                "cpu_percent": c.get("cpu_used_percent"),
                "mem_bytes": c.get("mem_used"),
                "mem_percent": c.get("mem_used_percent"),
            }

        return {
            "total": len(items),
            "sort": sort,
            "summary": raw.get("total_consume") or {},
            "items": [_norm(p) for p in items[:limit]],
        }

    async def services(self) -> dict:
        return await self._cache.get_or_set(
            "services", self._s.ttl_network, self._fetch_services
        )

    async def _fetch_services(self) -> dict:
        raw = await self._c.get("v1/taskmgr/services") or {}
        items = [
            {
                "name": s.get("name"),
                "id": s.get("id"),
                "app_id": s.get("appid"),
                "cpu_percent": (s.get("consume") or {}).get("cpu_used_percent"),
                "mem_bytes": (s.get("consume") or {}).get("mem_used"),
                "operable": bool(s.get("can_be_operated")),
            }
            for s in (raw.get("list") or [])
            if isinstance(s, dict)
        ]
        return {
            "total": len(items),
            "summary": raw.get("total_consume") or {},
            "items": items,
        }

    async def process_names(self) -> dict[str, int]:
        """进程名 → 实例数。

        实测 `v1/taskmgr/processes` 一次返回全量（本机 339 条，不分页），
        里面能看到 smbd / dockerd / nginx / sshd 等系统守护进程，
        而 `services()` 只列 UGOS 应用级服务（28 条，不含 smbd / nfsd）。
        """
        return await self._cache.get_or_set(
            "process_names", self._s.ttl_processes, self._fetch_process_names
        )

    async def _fetch_process_names(self) -> dict[str, int]:
        raw = await self._c.get("v1/taskmgr/processes") or {}
        counts: dict[str, int] = {}
        for p in raw.get("list") or []:
            if not isinstance(p, dict):
                continue
            name = (p.get("name") or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        return counts

    async def docker_engine(self) -> dict:
        """Docker 引擎状态。`GetEngineStatus` 返回 `{"result": "online"}`。"""
        return await self._cache.get_or_set(
            "docker_engine", self._s.ttl_network, self._fetch_docker_engine
        )

    async def _fetch_docker_engine(self) -> dict:
        raw = await self._c.get("v1/docker/view/GetEngineStatus") or {}
        status = raw.get("result") if isinstance(raw, dict) else None
        return {"status": status or "unknown", "healthy": status == "online"}

    # ------------------------------------------------------------------ 硬盘休眠

    async def power_config(self) -> dict:
        """`v1/hardware/power/config` 原始配置。

        同一个接口同时承载「硬盘休眠」与「电源计划」（`time_switches` / `power_boot` /
        `wake_on` / `enable_scheduled_power`），因此单独缓存一份给两个视图共用，
        避免一次巡检里重复请求。**只读**。
        """
        return await self._cache.get_or_set(
            "power_config", 60.0, lambda: self._c.get("v1/hardware/power/config") or {}
        )

    async def disk_sleep_policy(self) -> dict:
        """硬盘休眠策略（`v1/hardware/power/config`，实测可用）。

        字段语义来自字段名与前端「硬件与电源」页，未做写操作验证：
        `hard_drive_flag` 是否启用休眠、`hard_drive_unit`(M/H) + `hard_drive_time` 空闲时长。
        """
        return await self._cache.get_or_set("sleep_policy", 60.0, self._fetch_sleep_policy)

    async def _fetch_sleep_policy(self) -> dict:
        raw = await self.power_config()
        targets = raw.get("hard_drive_target_info") or {}
        usb_targets = raw.get("usb_hard_drive_target_info") or {}

        def _targets(info: dict) -> list[dict]:
            out: list[dict] = []
            for pool in info.get("pools") or []:
                if not isinstance(pool, dict):
                    continue
                out.append(
                    {
                        "kind": "pool",
                        "pool": pool.get("pool_name"),
                        "label": pool.get("pool_label"),
                        "selected": bool(pool.get("is_selected")),
                        "disks": [
                            {
                                "dev": d.get("dev_name"),
                                "label": d.get("disk_label"),
                                "selected": bool(d.get("is_selected")),
                                "support_idle": bool(d.get("support_idle")),
                                "reason": d.get("not_support_reason") or "",
                            }
                            for d in (pool.get("disk_in_pool") or [])
                            if isinstance(d, dict)
                        ],
                    }
                )
            for d in info.get("free_disks") or []:
                if not isinstance(d, dict):
                    continue
                out.append(
                    {
                        "kind": "disk",
                        "pool": None,
                        "label": d.get("disk_label"),
                        "selected": bool(d.get("is_selected")),
                        "disks": [
                            {
                                "dev": d.get("dev_name"),
                                "label": d.get("disk_label"),
                                "selected": bool(d.get("is_selected")),
                                "support_idle": bool(d.get("support_idle")),
                                "reason": d.get("not_support_reason") or "",
                            }
                        ],
                    }
                )
            return out

        unit = raw.get("hard_drive_unit") or ""
        minutes = None
        if isinstance(raw.get("hard_drive_time"), (int, float)):
            minutes = int(raw["hard_drive_time"]) * (60 if str(unit).upper().startswith("H") else 1)

        return {
            "enabled": bool(raw.get("hard_drive_flag")),
            "unit": unit or None,
            "raw_time": raw.get("hard_drive_time"),
            "idle_minutes": minutes,
            "strategy": raw.get("hard_drive_strategy"),
            "targets": _targets(targets),
            "scope": raw.get("hard_drive_scope") or {},
            "usb": {
                "enabled": bool(raw.get("usb_hard_drive_flag")),
                "unit": raw.get("usb_hard_drive_unit"),
                "raw_time": raw.get("usb_hard_drive_time"),
                "targets": _targets(usb_targets),
            },
            "sleep_log_enabled": bool(raw.get("enable_hard_drive_sleep_log")),
            "scheduled_power": {
                "enabled": bool(raw.get("enable_scheduled_power")),
                "switches": raw.get("time_switches") or [],
            },
        }

    async def disk_sleep_states(self) -> list[dict]:
        """每块盘的休眠状态。

        唯一可信信号是磁盘列表的 `is_standby`（布尔）：
        实测本机两块盘 `is_standby=false`、`status=1`。休眠时的 `status` 取值未观测到，
        因此不做推断，原样透出。
        """
        disks = (await self.storage()).get("disks") or []
        out: list[dict] = []
        for d in disks:
            standby = d.get("standby")
            state = "unknown" if standby is None else ("sleeping" if standby else "active")
            out.append(
                {
                    "name": d.get("name"),
                    "label": d.get("label"),
                    "slot": d.get("slot"),
                    "state": state,
                    "is_standby": standby,
                    "standby": standby,
                    "status": d.get("status"),
                    "temperature": d.get("temperature"),
                    "power_on_hours": d.get("power_on_hours"),
                    "pool": d.get("used_for"),
                    "model": d.get("model"),
                }
            )
        return out

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
            "processes": "v1/taskmgr/processes",
            "services": "v1/taskmgr/services",
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
        try:
            await self._c.get("v1/docker/view/ObtainOverviewInfo")
            caps["docker_usage"] = True
        except Exception:  # noqa: BLE001
            caps["docker_usage"] = False
        # 镜像接口只吃 POST，用 GET 探测会得到 9405
        try:
            await self._c.post(
                "v1/docker/image/ShowLocalImageV2", {"PageNum": 1, "PageSize": 1}
            )
            caps["images"] = True
        except Exception:  # noqa: BLE001
            caps["images"] = False
        # 快照需要 page_number/page_size，缺了会 9999
        try:
            await self._c.get(
                "v1/snapshot/folder/share", {"page_number": 1, "page_size": 1}
            )
            caps["snapshots"] = True
        except Exception:  # noqa: BLE001
            caps["snapshots"] = False
        # SMART 需要一块真实磁盘做探测
        caps["smart"] = False
        caps["benchmark"] = False
        try:
            disks = (await self.storage()).get("disks") or []
            target = next((d for d in disks if d.get("support_smart")), None)
            if target:
                await self.disk_smart(target.get("name") or "")
                caps["smart"] = True
                await self._c.get(
                    "v1/storage/disk/benchmark/info", {"disk": f"/dev/{target.get('name')}"}
                )
                caps["benchmark"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("SMART/测速探测失败：%s", exc)
        try:
            caps["ups_connected"] = bool((await self.system()).get("ups"))
        except Exception:  # noqa: BLE001
            caps["ups_connected"] = False

        # 第四批：会话 / 风扇 / 备份 / 电源计划 / 虚拟机
        try:
            await self._c.post("v1/user/online/list",
                               {"uid_list": [], "user_type": "user", "page": 1, "size": 1})
            caps["sessions"] = True
        except Exception:  # noqa: BLE001
            caps["sessions"] = False
        for name, path in (
            ("fan", "v1/hardware/fan/config"),
            ("backup", "v1/web/sync/task/list"),
            ("power_plan", "v1/hardware/power/config"),
        ):
            try:
                await self._c.get(path)
                caps[name] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("能力探测失败 %s: %s", name, exc)
                caps[name] = False
        # 虚拟机是独立应用，未安装时后端整个模块都是 404，只能看应用清单
        try:
            raw = await self._c.get("v1/desktop/module/list") or {}
            ids = [str(m.get("module_id") or "").lower() for m in (raw.get("result") or [])
                   if isinstance(m, dict)]
            caps["virtual_machine"] = any(
                "virtualmachine" in i or i.endswith(".vm") or ".kvm" in i for i in ids
            )
        except Exception:  # noqa: BLE001
            caps["virtual_machine"] = False
        return caps
