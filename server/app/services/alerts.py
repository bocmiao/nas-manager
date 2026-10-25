"""告警引擎：规则评估 + 状态机（去重、恢复通知）。

设计目标：
- 只在「新出现」和「恢复」两个时刻产生事件，供推送通道消费
- 所有阈值来自配置，不写死
- 规则独立、可扩展，新增规则只需加一个评估函数
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable

from ..config import Settings

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
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._active: dict[str, Alert] = {}
        self._lock = asyncio.Lock()
        self._events: list[AlertEvent] = []
        self._container_state: dict[str, bool] = {}
        self._initialized_containers = False
        self._suppressed: set[str] = set()

    # ---------------------------------------------------------------- 评估

    def _evaluate(self, snapshot: dict) -> dict[str, Alert]:
        found: dict[str, Alert] = {}
        s = self._s
        now = time.time()

        storage = snapshot.get("storage") or {}
        live = snapshot.get("live") or {}

        for disk in storage.get("disks") or []:
            temp = disk.get("temperature")
            if isinstance(temp, (int, float)) and temp >= s.alert_disk_temp:
                key = f"disk_temp:{disk.get('name')}"
                found[key] = Alert(
                    key=key,
                    scope="disk",
                    level="critical" if temp >= s.alert_disk_temp + 8 else "warning",
                    title=f"{disk.get('label') or disk.get('name')} 温度过高",
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
                    title=f"{disk.get('label') or disk.get('name')} 状态异常",
                    detail=f"磁盘状态码 {disk.get('status')}，建议立即检查 SMART 信息",
                    value=float(disk.get("status") or 0),
                    since=now,
                )

        for pool in storage.get("pools") or []:
            if not pool.get("healthy", True):
                key = f"pool_status:{pool.get('name')}"
                found[key] = Alert(
                    key=key,
                    scope="pool",
                    level="critical",
                    title=f"{pool.get('label') or pool.get('name')} 状态异常",
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
                    title=f"{pool.get('label') or pool.get('name')} 无冗余",
                    detail=f"{str(pool.get('level')).upper()} 模式，任一磁盘损坏将导致全部数据丢失",
                    since=now,
                )

        for vol in storage.get("volumes") or []:
            pct = vol.get("used_percent") or 0
            if pct >= s.alert_volume_used:
                key = f"volume_used:{vol.get('name')}"
                found[key] = Alert(
                    key=key,
                    scope="volume",
                    level="critical" if pct >= 97 else "warning",
                    title=f"{vol.get('label') or vol.get('name')} 空间不足",
                    detail=f"已用 {pct}%，阈值 {s.alert_volume_used}%（{vol.get('mount')}）",
                    value=float(pct),
                    threshold=s.alert_volume_used,
                    since=now,
                )

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

        dev_fan = (live.get("fans") or {}).get("device") or {}
        if dev_fan.get("speed") == 0 and dev_fan.get("status") not in (0, None):
            found["fan_stall"] = Alert(
                key="fan_stall",
                scope="system",
                level="warning",
                title="机箱风扇停转",
                detail="机箱风扇转速为 0，请检查散热",
                value=0.0,
                since=now,
            )

        return found

    # ---------------------------------------------------------------- 容器

    def _evaluate_containers(self, containers: Iterable[dict], now: float) -> dict[str, Alert]:
        found: dict[str, Alert] = {}
        for c in containers:
            name = c.get("name") or c.get("id")
            if not name:
                continue
            running = bool(c.get("running"))
            prev = self._container_state.get(name)
            self._container_state[name] = running
            # 首轮只建基线，避免启动瞬间误报
            if not self._initialized_containers or prev is None:
                continue
            if prev and not running and name not in self._suppressed:
                key = f"container_down:{name}"
                found[key] = Alert(
                    key=key,
                    scope="container",
                    level="warning",
                    title=f"容器 {name} 已停止",
                    detail=f"状态 {c.get('status')}，镜像 {c.get('image')}",
                    since=now,
                )
        self._initialized_containers = True
        return found

    # ---------------------------------------------------------------- 状态机

    async def evaluate(self, snapshot: dict) -> list[AlertEvent]:
        async with self._lock:
            now = time.time()
            found = self._evaluate(snapshot)
            found.update(self._evaluate_containers(snapshot.get("containers") or [], now))

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
        else:
            self._suppressed.discard(container_name)
