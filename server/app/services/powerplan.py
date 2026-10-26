"""电源计划（只读）：定时开关机、断电恢复、唤醒策略。

实测（详见 docs/api-verified.md §六）：
- 没有独立的「电源计划」接口：`v1/hardware/power/{plan,schedule,list,time_switch}` 全是 `9404`，
  `v1/power/*` 是 HTTP 404；计划表就在 `v1/hardware/power/config` 里
- `time_switches[]`：`{frequency:[1..7], time:"HH:MM", action:"open"|"close"}`
  星期编号来自前端 `convertDay`：**1=周一 … 5=周五，6=周六，7=周日**，
  另外 [1,2,3,4,5] 显示为「工作日」、[6,7] 显示为「周末」、全 7 天显示为「每天」
- `enable_scheduled_power` 定时开关机总开关；`power_boot` / `wake_on` 是「通电/唤醒」策略布尔位
  （前端字段名直译，中文提示文案在本机语言包里取不到，故不改写语义）
- 本机实测：`enable_scheduled_power=true`，一条「每天 08:00 开机」

**只读**：`v1/hardware/power/start` 会改计划，误改可能导致 NAS 不按预期启停，不实现。
"""
from __future__ import annotations

import time

from ..config import Settings
from .nas import NasService

WEEKDAY_LABELS = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}

ACTION_LABELS = {"open": "开机", "close": "关机", "on": "开机", "off": "关机"}


def days_label(days: list[int]) -> str:
    """按前端 convertDay 的规则给星期集合一个名字。"""
    ds = sorted({int(d) for d in days if isinstance(d, int) or str(d).isdigit()})
    if ds == [1, 2, 3, 4, 5]:
        return "工作日"
    if ds == [6, 7]:
        return "周末"
    if ds == [1, 2, 3, 4, 5, 6, 7]:
        return "每天"
    return "、".join(WEEKDAY_LABELS.get(d, f"?{d}") for d in ds) or "未设置"


def normalize_switch(sw: dict) -> dict:
    freq = sw.get("frequency") or []
    if isinstance(freq, (int, str)):
        freq = [freq]
    days = []
    for d in freq:
        try:
            days.append(int(d))
        except (TypeError, ValueError):
            continue
    action = str(sw.get("action") or "").lower()
    return {
        "days": days,
        "days_label": days_label(days),
        "time": sw.get("time"),
        "action": action or None,
        "action_label": ACTION_LABELS.get(action, action or "未知"),
    }


class PowerPlanService:
    def __init__(self, nas: NasService, settings: Settings) -> None:
        self._nas = nas
        self._s = settings

    async def plan(self) -> dict:
        raw = await self._nas.power_config()
        switches = [normalize_switch(x) for x in (raw.get("time_switches") or [])
                    if isinstance(x, dict)]
        switches.sort(key=lambda x: (x.get("time") or "", x.get("days") or []))

        boot = sorted((s for s in switches if s["action"] == "open"), key=lambda x: x["time"] or "")
        shutdown = sorted((s for s in switches if s["action"] == "close"), key=lambda x: x["time"] or "")

        return {
            "enabled": bool(raw.get("enable_scheduled_power")),
            "switches": switches,
            "boot": boot,
            "shutdown": shutdown,
            "next": self._next(switches),
            "power_boot": raw.get("power_boot"),
            "wake_on": raw.get("wake_on"),
            "weekday_note": "frequency 编号：1=周一 … 7=周日（前端 convertDay 语义）",
            "power_boot_note": "power_boot / wake_on 为前端同名字段直译（通电启动 / 唤醒），"
                               "中文文案在本机语言包中取不到，未改写语义",
            "read_only": True,
            "note": "只读。v1/hardware/power/start 会改计划，误改会导致 NAS 不按预期启停，中间层不实现。",
            "collected_at": int(time.time()),
        }

    @staticmethod
    def _next(switches: list[dict]) -> dict | None:
        """按「今天星期 + 计划时刻」估算下一次触发（本地时区，只做提示不做调度）。"""
        if not switches:
            return None
        now = time.localtime()
        cur_day = now.tm_wday + 1  # tm_wday: 0=周一 → 本系统的 1..7
        cur_min = now.tm_hour * 60 + now.tm_min
        best = None
        for sw in switches:
            t = sw.get("time") or ""
            if ":" not in t:
                continue
            try:
                hh, mm = (int(x) for x in t.split(":")[:2])
            except ValueError:
                continue
            start = hh * 60 + mm
            for d in sw.get("days") or []:
                delta = (d - cur_day) % 7
                if delta == 0 and start <= cur_min:
                    delta = 7
                # 距当前时刻的分钟数，用于排序与「还有多久」
                score = delta * 1440 + start - cur_min
                if best is None or score < best[0]:
                    best = (score, sw, d)
        if best is None:
            return None
        _, sw, d = best
        return {
            "at": sw["time"],
            "day": d,
            "day_label": WEEKDAY_LABELS.get(d),
            "action": sw["action"],
            "action_label": sw["action_label"],
            "in_hours": round(best[0] / 60, 1),
        }
