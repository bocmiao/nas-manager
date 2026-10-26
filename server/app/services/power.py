"""硬盘休眠监控与「频繁唤醒」判定。

数据来源（实测）：
- 休眠状态：`v2/storage/disk/list` 的 `is_standby`（布尔），这是 UGOS 唯一暴露的休眠信号
- 休眠策略：`v1/hardware/power/config` 的 `hard_drive_*` 字段（见 NasService.disk_sleep_policy）
- 单盘温度：`v2/storage/disk/list` 的 `temperature`（休眠时是否仍上报未观测到，原样透出）

局限性（写进文档，不粉饰）：
- 采样粒度 = 巡检间隔（默认 60 秒），盘在两次采样之间「睡下又醒来」会漏掉
- `v1/storage/disk/sleep` / `standby` / `hibernate` 等直连路径全部 9404，不存在按需查询的休眠接口
"""
from __future__ import annotations

import logging
import time
from collections import deque

from ..config import Settings

log = logging.getLogger("disksleep")

# 状态跳变保留 24 小时，够客户端画一条「今天醒了几次」的带
KEEP_SECONDS = 86400
MAX_TRANSITIONS = 500


class DiskSleepMonitor:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._state: dict[str, bool | None] = {}
        self._since: dict[str, float] = {}
        self._transitions: dict[str, deque[tuple[float, str, str]]] = {}
        self._wakes: dict[str, deque[float]] = {}
        self._last_observe: float | None = None
        self._samples = 0

    # ---------------------------------------------------------------- 采样

    def observe(self, disks: list[dict], ts: float | None = None) -> None:
        """喂一轮磁盘状态。`disks` 用 NasService.storage() 的归一化磁盘条目。"""
        now = ts or time.time()
        window = max(KEEP_SECONDS, self._s.alert_disk_wake_window)
        for d in disks:
            name = d.get("name")
            if not name:
                continue
            standby = d.get("standby")
            cur = None if standby is None else bool(standby)
            prev = self._state.get(name)
            if name not in self._state:
                # 首轮只建基线，不产生跳变
                self._state[name] = cur
                self._since[name] = now
                self._transitions[name] = deque(maxlen=MAX_TRANSITIONS)
                self._wakes[name] = deque()
                continue
            self._state[name] = cur
            if prev is None or cur is None or prev == cur:
                continue

            self._since[name] = now
            label_prev = "sleeping" if prev else "active"
            label_cur = "sleeping" if cur else "active"
            bucket = self._transitions.setdefault(name, deque(maxlen=MAX_TRANSITIONS))
            bucket.append((now, label_prev, label_cur))

            wakes = self._wakes.setdefault(name, deque())
            if prev and not cur:
                # 睡 → 醒：记一次唤醒
                wakes.append(now)

        # 清理过期数据
        for name in list(self._wakes):
            wakes = self._wakes[name]
            while wakes and now - wakes[0] > window:
                wakes.popleft()
            trans = self._transitions.get(name)
            if trans is not None:
                while trans and now - trans[0][0] > KEEP_SECONDS:
                    trans.popleft()

        self._last_observe = now
        self._samples += 1

    # ---------------------------------------------------------------- 查询

    def wakes(self, name: str, window: float | None = None) -> int:
        win = window or self._s.alert_disk_wake_window
        now = time.time()
        return sum(1 for t in self._wakes.get(name, ()) if now - t <= win)

    def state_of(self, name: str) -> str:
        val = self._state.get(name)
        if val is None:
            return "unknown"
        return "sleeping" if val else "active"

    def summary(self) -> dict:
        return {
            "samples": self._samples,
            "last_observe_at": int(self._last_observe) if self._last_observe else None,
            "disks": {k: self.state_of(k) for k in self._state},
        }

    def status(self, disks: list[dict], policy: dict | None = None) -> dict:
        """给接口用：把静态状态、跳变历史、唤醒统计拼成一份。"""
        now = time.time()
        window = self._s.alert_disk_wake_window
        rows = []
        for d in disks:
            name = d.get("name")
            if not name:
                continue
            trans = list(self._transitions.get(name, ()))
            wakes = [t for t in self._wakes.get(name, ()) if now - t <= window]
            since = self._since.get(name)
            rows.append(
                {
                    **d,
                    "state": self.state_of(name),
                    "state_since": int(since) if since else None,
                    "state_duration_seconds": int(now - since) if since else None,
                    "wakes_in_window": len(wakes),
                    "last_wake_at": int(wakes[-1]) if wakes else None,
                    "changes_24h": len(trans),
                    "recent_changes": [
                        {"ts": int(t), "from": a, "to": b} for t, a, b in trans[-10:]
                    ],
                }
            )

        over = [
            {"disk": r["name"], "label": r.get("label"), "wakes": r["wakes_in_window"]}
            for r in rows
            if r["wakes_in_window"] >= self._s.alert_disk_wake_count
        ]

        return {
            "policy": policy or {},
            "disks": rows,
            "frequency": {
                "window_seconds": window,
                "threshold": self._s.alert_disk_wake_count,
                "over_threshold": over,
            },
            "sampling": {
                "interval_seconds": max(10, self._s.poll_interval),
                "samples": self._samples,
                "last_observe_at": int(self._last_observe) if self._last_observe else None,
                "limitation": "只在巡检时采样，两次采样之间的「睡下又醒来」会漏计",
            },
            "state_source": "v2/storage/disk/list → is_standby",
            "collected_at": int(now),
        }
