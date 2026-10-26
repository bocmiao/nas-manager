"""风扇只读查询：当前转速、策略模式、温度-转速档位表。

实测（详见 docs/api-verified.md §六）：
- 转速：`v1/taskmgr/stat/overview` → `cpu_fan{speed,status}` / `device_fan{speed,status}`
  （status=1 表示该风扇在转；本机 CPU 风扇 385~409 RPM、机箱风扇 544 RPM）
- 策略模式：`GET v1/hardware/fan/config` → `mode`，取值来自前端 fanModal：
  **1=静音 / 2=自动 / 3=全速 / 4=自定义**（本机 mode=2 自动）
- 档位表：`sys_fan_table` / `cpu_fan_table`，本机均为空数组且 `support_custom=false`；
  前端逻辑里只有机型不在 `["dxp4800"]` 白名单时才给自定义曲线，DXP4800 Plus 只支持预设
- **目标转速没有任何接口暴露**，不猜
- 写接口确实存在（`GET v1/hardware/fan/start?mode=`、`POST v1/hardware/fan/setfan`），
  但改风扇策略可能损伤硬件，中间层**只读**，一行写代码都没有
"""
from __future__ import annotations

import logging
import time

from ..cache import TTLCache
from ..config import Settings
from .nas import NasService

log = logging.getLogger("fans")

# mode → (key, 中文名)，取自前端 fanModal 的预设列表与 hardwareAndPower 文案键
MODE_LABELS = {
    1: ("mute", "静音"),
    2: ("auto", "自动"),
    3: ("full_speed", "全速"),
    4: ("custom", "自定义"),
}


def _fan(node: dict | None) -> dict:
    node = node or {}
    speed = node.get("speed")
    status = node.get("status")
    return {
        "rpm": speed,
        "status": status,
        # 实测 status=1 = 风扇在转；0 = 未启用/无此风扇
        "running": status == 1,
        "present": isinstance(speed, (int, float)) and speed > 0,
    }


class FanService:
    def __init__(self, nas: NasService, cache: TTLCache, settings: Settings) -> None:
        self._nas = nas
        self._cache = cache
        self._s = settings

    @property
    def _c(self):
        return self._nas.client

    async def status(self) -> dict:
        return await self._cache.get_or_set("fan_status", self._s.ttl_fans, self._fetch)

    async def _fetch(self) -> dict:
        live = await self._nas.live()
        fans = live.get("fans") or {}
        cfg: dict = {}
        cfg_error = None
        try:
            cfg = await self._c.get("v1/hardware/fan/config") or {}
        except Exception as exc:  # noqa: BLE001 - 配置拿不到仍可给转速
            cfg_error = f"{type(exc).__name__}: {exc}"
            log.warning("读取风扇配置失败：%s", exc)

        mode_code = cfg.get("mode", cfg.get("result"))
        mode_key, mode_label = MODE_LABELS.get(mode_code, ("unknown", "未知"))

        sys_table = [x for x in (cfg.get("sys_fan_table") or []) if isinstance(x, (list, tuple))]
        cpu_table = [x for x in (cfg.get("cpu_fan_table") or []) if isinstance(x, (list, tuple))]
        has_curve = bool(sys_table or cpu_table) and bool(cfg.get("support_custom"))

        return {
            "mode": {
                "code": mode_code,
                "key": mode_key,
                "label": mode_label,
                "raw_result": cfg.get("result"),
            },
            "modes": [
                {"code": code, "key": key, "label": label, "current": code == mode_code}
                for code, (key, label) in sorted(MODE_LABELS.items())
            ],
            "speed": {
                "case": _fan(fans.get("device")),
                "cpu": _fan(fans.get("cpu")),
                "unit": "RPM",
            },
            "target_rpm": {
                "supported": False,
                "value": None,
                "reason": "UGOS 只暴露实际转速（stat/overview 的 cpu_fan/device_fan.speed），"
                          "任何端点都没有目标转速或 PWM 占空比字段",
            },
            "curve": {
                "supported": has_curve,
                "support_custom": bool(cfg.get("support_custom")),
                "sys_fan_table": sys_table,
                "cpu_fan_table": cpu_table,
                "reason": None if has_curve else
                "本机 support_custom=false 且 sys/cpu_fan_table 为空数组："
                "DXP4800 系列在前端只给「静音/自动/全速」预设，不下发自定义曲线",
            },
            "hardware_flags": {
                "has_sys_fan": cfg.get("has_sys_fan"),
                "has_cpu_fan": cfg.get("has_cpu_fan"),
                "note": "两个标志本机均为 false，但 stat/overview 同时报出两路转速，"
                        "含义未确认，故只原样透出、不据此判断风扇有无",
            },
            "config_error": cfg_error,
            "raw_config": cfg,
            "writable": False,
            "read_only": True,
            "note": "只读。改风扇策略（fan/start、fan/setfan）可能损伤硬件，中间层不实现。",
            "collected_at": int(time.time()),
        }
