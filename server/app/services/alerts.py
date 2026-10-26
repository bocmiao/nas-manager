"""告警引擎：规则评估 + 状态机（去重、恢复通知）。

设计目标：
- 只在「新出现」和「恢复」两个时刻产生事件，供推送通道消费
- 所有阈值来自配置，不写死
- 规则独立、可扩展，新增规则只需加一个评估函数
- 「相对量」规则（容器重启、网口降速、风扇）首次评估只建基线，不产生事件
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable

from ..config import Settings
from .power import DiskSleepMonitor

log = logging.getLogger("alerts")

LEVEL_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Alert:
    key: str
    scope: str
    level: str
    title: str
    detail: str
    value: float | None = None
    threshold: float | None = None
    since: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["since_ts"] = int(self.since)
        d["duration_seconds"] = int(time.time() - self.since)
        return d


@dataclass
class AlertEvent:
    """状态变化事件，推送通道只需要消费这个。"""
    action: str  # raised / resolved
    alert: Alert
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"action": self.action, "alert": self.alert.to_dict(), "ts": int(self.ts)}


class AlertEngine:
    def __init__(self, settings: Settings, sleep: DiskSleepMonitor | None = None) -> None:
        self._s = settings
        self._active: dict[str, Alert] = {}
        self._lock = asyncio.Lock()
        self._events: list[AlertEvent] = []
        self._suppressed: set[str] = set()
        self._sleep = sleep or DiskSleepMonitor(settings)

        # 相对量规则所需的上一轮状态
        self._container_state: dict[str, bool] = {}
        self._container_down_streak: dict[str, int] = {}
        self._container_restarts: dict[str, list[float]] = {}
        self._nic_peak_mbps: dict[str, int] = {}
        self._fan_baseline: dict[str, tuple] = {}
        self._primed = False

        # 第三批：容器「期望运行」基线 / 服务基线 / 网口错误计数
        self._container_run_streak: dict[str, int] = {}
        self._container_expected: dict[str, bool] = {}
        self._container_expected_at: dict[str, float] = {}
        self._service_streak: dict[str, int] = {}
        self._service_expected: dict[str, bool] = {}
        self._service_labels: dict[str, str] = {}
        self._service_missing_since: dict[str, float] = {}
        self._nic_counters: dict[str, dict] = {}

    # ---------------------------------------------------------------- 规则

    def _evaluate(self, snapshot: dict) -> dict[str, Alert]:
        found: dict[str, Alert] = {}
        s = self._s
        now = time.time()

        storage = snapshot.get("storage") or {}
        live = snapshot.get("live") or {}
        system = snapshot.get("system") or {}

        found.update(self._check_disks(storage, now))
        found.update(self._check_pools(storage, now))
        found.update(self._check_volumes(storage, now))
        found.update(self._check_host(live, now))
        found.update(self._check_ups(system.get("ups") or {}, now))
        found.update(self._check_smart(snapshot.get("smart") or [], now))
        return found

    # -- 磁盘温度 / 状态 ---------------------------------------------------

    def _check_disks(self, storage: dict, now: float) -> dict[str, Alert]:
        found: dict[str, Alert] = {}
        s = self._s
        for disk in storage.get("disks") or []:
            label = disk.get("label") or disk.get("name")
            temp = disk.get("temperature")
            if isinstance(temp, (int, float)) and temp >= s.alert_disk_temp:
                key = f"disk_temp:{disk.get('name')}"
                found[key] = Alert(
                    key=key,
                    scope="disk",
                    level="critical" if temp >= s.alert_disk_temp_critical else "warning",
                    title=f"{label} 温度过高",
                    detail=f"当前 {temp}°C，阈值 {s.alert_disk_temp}°C（{disk.get('model')}）",
                    value=float(temp),
                    threshold=s.alert_disk_temp,
                    since=now,
                )
            # 磁盘健康：status 0/1 均属正常，其余告警
            if disk.get("healthy") is False:
                key = f"disk_status:{disk.get('name')}"
                found[key] = Alert(
                    key=key,
                    scope="disk",
                    level="critical",
                    title=f"{label} 状态异常",
                    detail=f"磁盘状态码 {disk.get('status')}，建议立即检查 SMART 信息",
                    value=float(disk.get("status") or 0),
                    since=now,
                )
        return found

    # -- 存储池 ------------------------------------------------------------

    def _check_pools(self, storage: dict, now: float) -> dict[str, Alert]:
        found: dict[str, Alert] = {}
        for pool in storage.get("pools") or []:
            label = pool.get("label") or pool.get("name")
            if not pool.get("healthy", True):
                key = f"pool_status:{pool.get('name')}"
                found[key] = Alert(
                    key=key,
                    scope="pool",
                    level="critical",
                    title=f"{label} 状态异常",
                    detail=f"存储池状态码 {pool.get('status')}，RAID 级别 {str(pool.get('level')).upper()}",
                    value=float(pool.get("status") or 0),
                    since=now,
                )
            if pool.get("no_redundancy"):
                key = f"pool_no_redundancy:{pool.get('name')}"
                found[key] = Alert(
                    key=key,
                    scope="pool",
                    level="info",
                    title=f"{label} 无冗余",
                    detail=f"{str(pool.get('level')).upper()} 模式，任一磁盘损坏将导致全部数据丢失",
                    since=now,
                )
        return found

    # -- 卷容量 ------------------------------------------------------------

    def _check_volumes(self, storage: dict, now: float) -> dict[str, Alert]:
        found: dict[str, Alert] = {}
        s = self._s
        for vol in storage.get("volumes") or []:
            pct = vol.get("used_percent") or 0
            if pct >= s.alert_volume_used:
                key = f"volume_used:{vol.get('name')}"
                found[key] = Alert(
                    key=key,
                    scope="volume",
                    level="critical" if pct >= s.alert_volume_used_critical else "warning",
                    title=f"{vol.get('label') or vol.get('name')} 空间不足",
                    detail=f"已用 {pct}%，阈值 {s.alert_volume_used}%（{vol.get('mount')}）",
                    value=float(pct),
                    threshold=s.alert_volume_used,
                    since=now,
                )
            # inode：UGOS 目前不返回该指标，字段为 None 时整条规则静默跳过
            inode = vol.get("inode_used_percent")
            if isinstance(inode, (int, float)) and inode >= s.alert_volume_inode:
                key = f"volume_inode:{vol.get('name')}"
                found[key] = Alert(
                    key=key,
                    scope="volume",
                    level="critical" if inode >= s.alert_volume_inode_critical else "warning",
                    title=f"{vol.get('label') or vol.get('name')} inode 接近耗尽",
                    detail=f"inode 已用 {inode}%，阈值 {s.alert_volume_inode}%（{vol.get('mount')}）",
                    value=float(inode),
                    threshold=s.alert_volume_inode,
                    since=now,
                )
        return found

    # -- CPU / 内存 / 风扇 / UPS -------------------------------------------

    def _check_host(self, live: dict, now: float) -> dict[str, Alert]:
        found: dict[str, Alert] = {}
        s = self._s

        cpu = live.get("cpu") or {}
        cpu_temp = cpu.get("temp")
        if isinstance(cpu_temp, (int, float)) and cpu_temp >= s.alert_cpu_temp:
            found["cpu_temp"] = Alert(
                key="cpu_temp",
                scope="system",
                level="critical",
                title="CPU 温度过高",
                detail=f"当前 {cpu_temp}°C，阈值 {s.alert_cpu_temp}°C",
                value=float(cpu_temp),
                threshold=s.alert_cpu_temp,
                since=now,
            )

        cpu_used = cpu.get("used_percent")
        if isinstance(cpu_used, (int, float)) and cpu_used >= s.alert_cpu_used:
            found["cpu_used"] = Alert(
                key="cpu_used",
                scope="system",
                level="warning",
                title="CPU 持续高负载",
                detail=f"当前 {cpu_used}%，阈值 {s.alert_cpu_used}%",
                value=float(cpu_used),
                threshold=s.alert_cpu_used,
                since=now,
            )

        mem_used = (live.get("memory") or {}).get("used_percent")
        if isinstance(mem_used, (int, float)) and mem_used >= s.alert_mem_used:
            found["mem_used"] = Alert(
                key="mem_used",
                scope="system",
                level="warning",
                title="内存占用过高",
                detail=f"当前 {mem_used:.1f}%，阈值 {s.alert_mem_used}%",
                value=float(mem_used),
                threshold=s.alert_mem_used,
                since=now,
            )

        found.update(self._check_fans((live.get("fans") or {}), now))
        return found

    def _check_fans(self, fans: dict, now: float) -> dict[str, Alert]:
        """风扇异常。

        `status` 实测语义：1 = 风扇在转、0 = 未启用/无风扇。
        因此只在「状态为启用但转速掉到阈值以下」时告警，
        否则像本机 CPU 风扇（status=0 speed=0，实际无此风扇）会全程误报。
        """
        found: dict[str, Alert] = {}
        min_rpm = self._s.alert_fan_min_rpm
        names = {"cpu": "CPU 风扇", "device": "机箱风扇"}
        for slot, info in fans.items():
            if not isinstance(info, dict):
                continue
            speed = info.get("speed")
            status = info.get("status")
            if status != 1 or not isinstance(speed, (int, float)):
                continue
            if speed < min_rpm:
                key = f"fan_stall:{slot}"
                found[key] = Alert(
                    key=key,
                    scope="system",
                    level="warning",
                    title=f"{names.get(slot, slot)} 转速异常",
                    detail=f"转速 {int(speed)} RPM，低于阈值 {min_rpm} RPM",
                    value=float(speed),
                    threshold=float(min_rpm),
                    since=now,
                )
        return found

    def _check_ups(self, ups: dict, now: float) -> dict[str, Alert]:
        """UPS 断电（切电池）告警。

        ⚠️ 本机未接 UPS，`hardware.ups` 恒为 null，字段名未经真机验证。
        这里按常见命名做多路兜底，任一路命中即判定，取不到就不告警（宁漏勿错）。
        """
        if not isinstance(ups, dict) or not ups:
            return {}
        on_battery = None
        for key in ("on_battery", "onBattery", "is_battery", "battery_mode"):
            if key in ups:
                on_battery = bool(ups.get(key))
                break
        if on_battery is None:
            for key in ("power_status", "ac_status", "status", "state"):
                if key not in ups:
                    continue
                val = ups.get(key)
                text = str(val).lower()
                if text in ("battery", "onbattery", "on_battery", "discharging", "1"):
                    on_battery = True
                elif text in ("online", "ac", "normal", "charging", "0"):
                    on_battery = False
                break
        if on_battery is not True:
            return {}

        charge = None
        for key in ("battery_charge", "battery_level", "charge", "capacity", "battery_percent"):
            if isinstance(ups.get(key), (int, float)):
                charge = float(ups[key])
                break
        runtime = None
        for key in ("remain_time", "runtime", "battery_runtime", "time_left"):
            if isinstance(ups.get(key), (int, float)):
                runtime = float(ups[key])
                break

        detail = "市电中断，NAS 已切换到 UPS 电池供电"
        if charge is not None:
            detail += f"，电量 {charge:.0f}%"
        if runtime is not None:
            detail += f"，预计续航 {int(runtime // 60)} 分钟"
        return {
            "ups_on_battery": Alert(
                key="ups_on_battery",
                scope="system",
                level="critical",
                title="UPS 已切电池供电",
                detail=detail,
                value=charge,
                since=now,
            )
        }

    # -- SMART -------------------------------------------------------------

    def _check_smart(self, smart_list: list, now: float) -> dict[str, Alert]:
        found: dict[str, Alert] = {}
        s = self._s
        for smart in smart_list:
            if not isinstance(smart, dict) or not smart.get("supported", True):
                continue
            disk = smart.get("disk") or {}
            name = disk.get("name")
            label = disk.get("label") or name
            counters = smart.get("counters") or {}
            key_items = smart.get("key_items") or {}

            def num(field_name: str) -> int:
                v = counters.get(field_name)
                return int(v) if isinstance(v, (int, float)) else 0

            problems: list[str] = []
            realloc = num("reallocated_sectors")
            pending = num("pending_sectors")
            offline = num("offline_uncorrectable")
            reported = num("reported_uncorrectable")
            crc = num("udma_crc_errors")

            if s.alert_smart_reallocated and realloc >= s.alert_smart_reallocated:
                problems.append(f"重映射扇区 {realloc}")
            if s.alert_smart_pending and pending >= s.alert_smart_pending:
                problems.append(f"待映射扇区 {pending}")
            if s.alert_smart_offline_uncorrectable and offline >= s.alert_smart_offline_uncorrectable:
                problems.append(f"脱机不可校正 {offline}")
            if reported > 0:
                problems.append(f"无法校正错误 {reported}")

            health = smart.get("health")
            if health in ("warning", "failed"):
                problems.append(f"SMART 自评 {health}")
            if crc > 0:
                # CRC 错误通常指向线缆/接口，单独一条 info 级
                key = f"disk_smart_crc:{name}"
                found[key] = Alert(
                    key=key,
                    scope="disk",
                    level="info",
                    title=f"{label} 接口 CRC 错误",
                    detail=f"UDMA CRC 错误计数 {crc}，多为线缆或背板接触问题",
                    value=float(crc),
                    since=now,
                )

            if problems:
                key = f"disk_smart:{name}"
                found[key] = Alert(
                    key=key,
                    scope="disk",
                    level="critical" if health == "failed" else "warning",
                    title=f"{label} SMART 异常",
                    detail="；".join(problems) + f"（通电 {key_items.get('power_on_hours')} 小时）",
                    value=float(realloc + pending + offline),
                    since=now,
                )
        return found

    # ---------------------------------------------------------------- 容器

    def _evaluate_containers(self, containers: Iterable[dict], now: float) -> dict[str, Alert]:
        """容器规则，三条互补、互不重复：

        - `container_restart_loop`：窗口内「停→起」次数达阈值，或处于 Docker 重启退避
        - `container_expected_down`：连续 N 轮 running 建立「期望运行」基线后转为停止，
          且**连续 2 轮**确认已停止（避免 Docker 自动重启的瞬间误判）
        - `container_down`：**未**建立基线的容器（刚部署、跑了不到 N 轮就挂）转为停止

        `container_expected_down` 与另外两条互斥：
        命中 `container_restart_loop` 时不再发停止类告警；建立基线的容器只发 expected_down。
        """
        found: dict[str, Alert] = {}
        s = self._s
        seen: set[str] = set()
        for c in containers:
            name = c.get("name") or c.get("id")
            if not name:
                continue
            seen.add(name)
            running = bool(c.get("running"))
            restarting = bool(c.get("restarting"))
            prev = self._container_state.get(name)
            self._container_state[name] = running

            # 连续 running 轮数 → 期望运行基线（一旦建立就保持，重启后仍期望它继续跑）
            if running:
                self._container_run_streak[name] = self._container_run_streak.get(name, 0) + 1
                if (
                    not self._container_expected.get(name)
                    and self._container_run_streak[name] >= s.alert_container_expected_polls
                ):
                    self._container_expected[name] = True
                    self._container_expected_at[name] = now
            else:
                self._container_run_streak[name] = 0

            # 首轮只建基线，不计入停止计数
            if not self._primed or prev is None:
                self._container_down_streak[name] = 0
                continue

            if prev is False and running:
                bucket = self._container_restarts.setdefault(name, [])
                bucket.append(now)
                self._container_restarts[name] = [
                    t for t in bucket if now - t <= s.alert_container_restart_window
                ]

            if running:
                self._container_down_streak[name] = 0
            elif prev is True:
                # 刚观察到「在跑 → 停止」，开始计数
                self._container_down_streak[name] = 1
            elif self._container_down_streak.get(name, 0) > 0:
                self._container_down_streak[name] += 1
            # 启动前就处于停止状态的容器不计入，避免服务重启后批量误报

            bucket = [
                t for t in self._container_restarts.get(name, [])
                if now - t <= s.alert_container_restart_window
            ]
            self._container_restarts[name] = bucket
            restart_loop = restarting or len(bucket) >= s.alert_container_restart_count

            if restart_loop and name not in self._suppressed:
                key = f"container_restart_loop:{name}"
                if restarting:
                    detail = (
                        f"容器处于 Docker 重启退避状态，本窗口已观察到 {len(bucket)} 次重启"
                        f"，镜像 {c.get('image')}"
                    )
                    level = "critical" if len(bucket) >= s.alert_container_restart_count else "warning"
                else:
                    detail = (
                        f"{s.alert_container_restart_window // 60} 分钟内重启 {len(bucket)} 次"
                        f"，镜像 {c.get('image')}"
                    )
                    level = "warning"
                found[key] = Alert(
                    key=key,
                    scope="container",
                    level=level,
                    title=f"容器 {name} 反复重启",
                    detail=detail,
                    value=float(len(bucket)),
                    threshold=float(s.alert_container_restart_count),
                    since=now,
                )

            if name in self._suppressed or running:
                continue
            # 反复重启已覆盖该容器，不再叠加停止类告警
            if restart_loop:
                continue

            if self._container_expected.get(name) and self._container_down_streak.get(name, 0) >= 2:
                # 连续 2 轮确认已停止：避免 Docker 自动重启的瞬间被误判成「停止」
                since = self._container_expected_at.get(name, now)
                key = f"container_expected_down:{name}"
                found[key] = Alert(
                    key=key,
                    scope="container",
                    level="warning",
                    title=f"容器 {name} 应运行但已停止",
                    detail=(
                        f"已连续运行 {s.alert_container_expected_polls} 轮以上并建立期望基线"
                        f"（{int((now - since) // 60)} 分钟前确认），当前状态 {c.get('status')}，"
                        f"非用户主动停止。镜像 {c.get('image')}"
                    ),
                    value=float(self._container_down_streak.get(name, 0)),
                    since=now,
                )
                continue

            if self._container_down_streak.get(name, 0) >= 2:
                key = f"container_down:{name}"
                found[key] = Alert(
                    key=key,
                    scope="container",
                    level="warning",
                    title=f"容器 {name} 已停止",
                    detail=f"状态 {c.get('status')}，镜像 {c.get('image')}",
                    since=now,
                )

        self._primed = True
        for name in list(self._container_state):
            if name not in seen:
                self._container_state.pop(name, None)
                self._container_down_streak.pop(name, None)
                self._container_restarts.pop(name, None)
                self._container_run_streak.pop(name, None)
                self._container_expected.pop(name, None)
                self._container_expected_at.pop(name, None)
        return found

    # ---------------------------------------------------------------- 网口

    def _check_nics(self, nics: list[dict], now: float) -> dict[str, Alert]:
        """网口降速。

        两种情况告警：
        1. 协商速率低于配置下限（`ALERT_NIC_MIN_MBPS`，0 表示不启用）
        2. 相较本进程运行期间的历史峰值掉档（例如 2.5G 掉到 1G，多为线缆/交换机问题）
        """
        found: dict[str, Alert] = {}
        min_mbps = self._s.alert_nic_min_mbps
        for nic in nics:
            name = nic.get("name")
            speed = nic.get("speed_mbps")
            if not name or not isinstance(speed, int) or speed <= 0:
                continue
            peak = self._nic_peak_mbps.get(name)
            if peak is None:
                # 首轮只建基线
                self._nic_peak_mbps[name] = speed
                continue
            self._nic_peak_mbps[name] = max(peak, speed)

            hit_config = bool(min_mbps) and speed < min_mbps
            hit_drop = speed < peak
            if hit_config or hit_drop:
                key = f"nic_speed:{name}"
                reason = (
                    f"低于配置下限 {min_mbps} Mbps" if hit_config
                    else f"较历史峰值 {peak} Mbps 降速"
                )
                found[key] = Alert(
                    key=key,
                    scope="system",
                    level="warning",
                    title=f"网口 {name} 速率下降",
                    detail=f"当前协商 {speed} Mbps（{nic.get('duplex') or '未知'}双工），{reason}",
                    value=float(speed),
                    threshold=float(min_mbps or peak),
                    since=now,
                )
        return found

    def _check_nic_errors(self, nics: list[dict], now: float) -> dict[str, Alert]:
        """网口错误包增长。

        UGOS 实测不暴露 rx/tx 错误与丢弃计数（字段为 None），此时整条规则静默跳过；
        若将来固件补上计数，这里按「窗口内增量」判定，防止把累计值当增量误报。
        """
        found: dict[str, Alert] = {}
        s = self._s
        window = max(60, s.alert_nic_error_window)
        fields = ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped")
        for nic in nics:
            name = nic.get("name")
            if not name:
                continue
            cur = {k: nic.get(k) for k in fields}
            if all(v is None for v in cur.values()):
                continue
            prev = self._nic_counters.get(name)
            self._nic_counters[name] = {**cur, "ts": now}
            if not prev:
                continue
            if now - prev.get("ts", now) > window * 3:
                # 采样间隔过大（进程重启/长时间不可达），重新取基线
                continue
            deltas = {
                k: int(v - prev[k])
                for k, v in cur.items()
                if isinstance(v, (int, float)) and isinstance(prev.get(k), (int, float)) and v >= prev[k]
            }
            total = sum(deltas.values())
            if total >= s.alert_nic_error_delta:
                key = f"nic_errors:{name}"
                detail = "、".join(f"{k} +{v}" for k, v in deltas.items() if v)
                found[key] = Alert(
                    key=key,
                    scope="system",
                    level="warning",
                    title=f"网口 {name} 错误包增长异常",
                    detail=f"{window // 60} 分钟内 {detail}，阈值 {s.alert_nic_error_delta}",
                    value=float(total),
                    threshold=float(s.alert_nic_error_delta),
                    since=now,
                )
        return found

    # ---------------------------------------------------------------- 服务

    def _evaluate_services(self, snapshot: dict, now: float) -> dict[str, Alert]:
        """关键服务未运行。

        两条判定：
        1. **基线型**：连续 N 轮出现在服务/进程清单里 → 认定「期望运行」，
           之后消失则告警。首轮只建基线，不会因为 NFS 本来就没开而误报
        2. **强制型**：`ALERT_REQUIRED_SERVICES` 里点名的进程，缺失即告警（绝对值）
        另外 Docker 引擎状态是绝对判定（`GetEngineStatus` 非 online 即 critical）。
        """
        sh = snapshot.get("service_health") or {}
        if not sh:
            return {}
        found: dict[str, Alert] = {}
        s = self._s

        entries: dict[str, tuple[str, bool]] = {}
        for svc in (sh.get("ugos_services") or {}).get("items") or []:
            sid = svc.get("id")
            if not sid:
                continue
            key = f"service:ugos:{sid}"
            self._service_labels[key] = svc.get("label") or svc.get("name") or sid
            # taskmgr/services 只列「在跑」的服务，出现在清单里就等于 present
            entries[key] = (self._service_labels[key], True)
        for item in (sh.get("system_processes") or {}).get("watched") or []:
            name = item.get("name")
            if not name:
                continue
            key = f"service:proc:{name}"
            self._service_labels[key] = item.get("label") or name
            entries[key] = (self._service_labels[key], bool(item.get("present")))

        # UGOS 服务停掉后直接从清单里消失，必须把「上一轮还在」的键补成本轮缺失
        for key in list(self._service_expected):
            if key not in entries:
                label = self._service_labels.get(key) or key.rsplit(":", 1)[1]
                entries[key] = (label, False)

        required = {x.strip() for x in (s.alert_required_services or "").split(",") if x.strip()}

        for key, (_label, present) in entries.items():
            if present:
                self._service_streak[key] = self._service_streak.get(key, 0) + 1
                if self._service_streak[key] >= s.alert_service_expected_polls:
                    self._service_expected[key] = True
                self._service_missing_since.pop(key, None)
            else:
                self._service_streak[key] = 0
                if self._service_expected.get(key):
                    self._service_missing_since.setdefault(key, now)

        for key, (label, present) in entries.items():
            if present:
                continue
            ident = key.rsplit(":", 1)[1]
            hard = key.startswith("service:proc:") and ident in required
            if not (hard or self._service_expected.get(key)):
                continue
            missing_at = self._service_missing_since.get(key, now)
            alert_key = f"service_down:{ident}"
            found[alert_key] = Alert(
                key=alert_key,
                scope="system",
                level="critical" if hard else "warning",
                title=f"关键服务未运行：{label}",
                detail=(
                    f"{'配置要求常驻' if hard else '此前连续多轮运行并已建立基线'}"
                    f"，已缺失 {int((now - missing_at) // 60)} 分钟（标识 {ident}）"
                ),
                since=missing_at,
            )

        engine = sh.get("docker_engine") or {}
        status = engine.get("status")
        if status and status not in ("unknown", "online"):
            found["service_down:docker-engine"] = Alert(
                key="service_down:docker-engine",
                scope="system",
                level="critical",
                title="Docker 引擎未在运行",
                detail=f"GetEngineStatus 返回 {status}，容器全部不可用",
                since=now,
            )
        return found

    # ---------------------------------------------------------------- 磁盘休眠

    def _check_disk_wakes(self, disks: list[dict], now: float) -> dict[str, Alert]:
        """磁盘频繁唤醒：窗口内「睡 → 醒」次数达阈值。

        计数由 DiskSleepMonitor 维护，采样粒度 = 巡检间隔，两次采样之间的短时唤醒会漏计
        （已在接口说明里写明）。首轮 observe 只建基线，因此不会误报。
        """
        found: dict[str, Alert] = {}
        s = self._s
        for d in disks:
            name = d.get("name")
            if not name:
                continue
            wakes = self._sleep.wakes(name)
            if wakes < s.alert_disk_wake_count:
                continue
            label = d.get("label") or name
            key = f"disk_wake_freq:{name}"
            found[key] = Alert(
                key=key,
                scope="disk",
                level="warning",
                title=f"{label} 频繁唤醒",
                detail=(
                    f"{s.alert_disk_wake_window // 60} 分钟内被唤醒 {wakes} 次，"
                    f"阈值 {s.alert_disk_wake_count} 次；建议检查休眠策略与后台索引/缩略图任务"
                ),
                value=float(wakes),
                threshold=float(s.alert_disk_wake_count),
                since=now,
            )
        return found

    # ---------------------------------------------------------------- 状态机

    async def evaluate(self, snapshot: dict) -> list[AlertEvent]:
        async with self._lock:
            now = time.time()
            storage = snapshot.get("storage") or {}
            disks = storage.get("disks") or []
            # 先采样休眠状态（幂等：同一状态重复 observe 不产生跳变）
            if disks:
                self._sleep.observe(disks, now)

            found = self._evaluate(snapshot)
            found.update(self._evaluate_containers(snapshot.get("containers") or [], now))
            system = snapshot.get("system") or {}
            found.update(self._check_nics(system.get("nics") or [], now))
            found.update(self._check_nic_errors(system.get("nics") or [], now))
            found.update(self._evaluate_services(snapshot, now))
            found.update(self._check_disk_wakes(disks, now))

            events: list[AlertEvent] = []

            for key, alert in found.items():
                existing = self._active.get(key)
                if existing is None:
                    self._active[key] = alert
                    events.append(AlertEvent("raised", alert))
                    log.warning("告警：%s — %s", alert.title, alert.detail)
                else:
                    # 保留首次发生时间，更新数值与描述
                    existing.value = alert.value
                    existing.detail = alert.detail
                    existing.level = alert.level

            for key in [k for k in self._active if k not in found]:
                alert = self._active.pop(key)
                events.append(AlertEvent("resolved", alert))
                log.info("告警恢复：%s", alert.title)

            self._events.extend(events)
            if len(self._events) > 500:
                self._events = self._events[-500:]
            return events

    def active(self) -> list[Alert]:
        return sorted(
            self._active.values(),
            key=lambda a: (-LEVEL_ORDER.get(a.level, 0), a.since),
        )

    def history(self, limit: int = 100) -> list[AlertEvent]:
        return self._events[-limit:]

    def summary(self) -> dict:
        active = self.active()
        counts = {"critical": 0, "warning": 0, "info": 0}
        for a in active:
            counts[a.level] = counts.get(a.level, 0) + 1
        return {
            "counts": counts,
            "total": len(active),
            "highest": active[0].level if active else None,
        }

    def suppress(self, container_name: str, suppressed: bool = True) -> None:
        """用户主动停止的容器不再告警。"""
        if suppressed:
            self._suppressed.add(container_name)
            self._active.pop(f"container_down:{container_name}", None)
            self._active.pop(f"container_restart_loop:{container_name}", None)
            self._active.pop(f"container_expected_down:{container_name}", None)
        else:
            self._suppressed.discard(container_name)

    # ---------------------------------------------------------------- 基线透视

    def container_baseline(self) -> dict:
        """容器「期望运行」基线的当前状态，供 `/containers/baseline` 与排查误报用。"""
        items = []
        for name in sorted(self._container_expected):
            if not self._container_expected.get(name):
                continue
            items.append(
                {
                    "name": name,
                    "expected_running": True,
                    "confirmed_at": (
                        int(self._container_expected_at[name])
                        if name in self._container_expected_at else None
                    ),
                    "run_streak": self._container_run_streak.get(name, 0),
                    "currently_running": self._container_state.get(name),
                    "suppressed": name in self._suppressed,
                    "restart_events_in_window": len(self._container_restarts.get(name, [])),
                }
            )
        return {
            "total": len(items),
            "required_polls": self._s.alert_container_expected_polls,
            "suppressed": sorted(self._suppressed),
            "items": items,
            "note": "连续 N 轮巡检 running 即认定「期望运行」；"
                    "命中后停止会发 container_expected_down，与 container_restart_loop 互斥。",
        }

    def service_baseline(self) -> dict:
        items = [
            {
                "service": key.split(":", 1)[1],
                "label": self._service_labels.get(key, ""),
                "expected_running": True,
                "present_streak": self._service_streak.get(key, 0),
                "missing_since": (
                    int(self._service_missing_since[key])
                    if key in self._service_missing_since else None
                ),
            }
            for key, expected in self._service_expected.items()
            if expected
        ]
        return {
            "total": len(items),
            "required_polls": self._s.alert_service_expected_polls,
            "items": items,
        }
