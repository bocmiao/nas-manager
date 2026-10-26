"""历史采样：内存环形缓冲，给客户端画曲线用。

- 默认 60 秒一采、每指标保留 360 点（6 小时），均可由配置覆盖
- **进程重启即清空**，响应里的 `since` 是缓冲里最早一个点的真实时间戳，
  客户端据此判断曲线起点，不要假设它等于"6 小时前"
- 采样失败不写空点，只记 `last_error`；客户端可用 `stale` 判断曲线是否停滞
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from ..config import Settings
from .nas import NasService

log = logging.getLogger("history")

# 指标定义：unit 用于客户端直接渲染，per_disk 表示按磁盘分别存
METRICS: dict[str, dict] = {
    "cpu": {"unit": "%", "label": "CPU 使用率"},
    "mem": {"unit": "%", "label": "内存使用率"},
    "cpu_temp": {"unit": "°C", "label": "CPU 温度"},
    "fan": {"unit": "RPM", "label": "机箱风扇转速"},
    "net_tx": {"unit": "B/s", "label": "网口发送速率"},
    "net_rx": {"unit": "B/s", "label": "网口接收速率"},
    "disk_temp": {"unit": "°C", "label": "磁盘温度", "per_disk": True},
    # 1 = 休眠（is_standby），0 = 工作。休眠状态切换即曲线上的台阶
    "disk_sleep": {"unit": "", "label": "磁盘休眠状态（1=休眠）", "per_disk": True},
}


class HistoryRecorder:
    def __init__(self, nas: NasService, settings: Settings) -> None:
        self._nas = nas
        self._s = settings
        self._max = max(10, settings.history_points)
        self._series: dict[str, deque[tuple[int, float]]] = {
            k: deque(maxlen=self._max) for k, v in METRICS.items() if not v.get("per_disk")
        }
        self._disk: dict[str, deque[tuple[int, float]]] = {}
        self._sleep: dict[str, deque[tuple[int, float]]] = {}
        self._started_at = int(time.time())
        self._last_sample_at: int | None = None
        self._last_error: str | None = None
        self._samples = 0

    # ---------------------------------------------------------------- 采样

    async def sample_once(self) -> dict:
        """采一次样。单指标失败不影响其它指标。"""
        ts = int(time.time())
        try:
            live = await self._nas.live()
        except Exception as exc:  # noqa: BLE001 - 采样不能中断
            self._last_error = f"live: {exc}"
            log.warning("历史采样（实时指标）失败：%s", exc)
            live = {}

        if live:
            self._push("cpu", ts, (live.get("cpu") or {}).get("used_percent"))
            self._push("mem", ts, (live.get("memory") or {}).get("used_percent"))
            self._push("cpu_temp", ts, (live.get("cpu") or {}).get("temp"))
            self._push("fan", ts, ((live.get("fans") or {}).get("device") or {}).get("speed"))
            self._push("net_tx", ts, (live.get("net") or {}).get("send_bps"))
            self._push("net_rx", ts, (live.get("net") or {}).get("recv_bps"))

        try:
            storage = await self._nas.storage()
            for d in storage.get("disks") or []:
                key = d.get("label") or d.get("name")
                if not key:
                    continue
                series = self._disk.get(key)
                if series is None:
                    series = deque(maxlen=self._max)
                    self._disk[key] = series
                temp = d.get("temperature")
                if isinstance(temp, (int, float)):
                    series.append((ts, float(temp)))

                # 休眠状态变化：is_standby 布尔 → 1/0，台阶即状态切换
                standby = d.get("standby")
                if isinstance(standby, bool):
                    sleep_series = self._sleep.get(key)
                    if sleep_series is None:
                        sleep_series = deque(maxlen=self._max)
                        self._sleep[key] = sleep_series
                    sleep_series.append((ts, 1.0 if standby else 0.0))
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"storage: {exc}"
            log.warning("历史采样（磁盘温度/休眠）失败：%s", exc)

        self._last_sample_at = ts
        self._samples += 1
        return {"ts": ts, "samples": self._samples}

    def _push(self, metric: str, ts: int, value) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        self._series[metric].append((ts, float(value)))

    async def run(self) -> None:
        interval = max(10, self._s.history_interval)
        log.info("历史采样启动：每 %ds 一次，保留 %d 点", interval, self._max)
        while True:
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 后台任务不能退出
                self._last_error = "sample loop error"
                log.exception("历史采样循环异常")
            await asyncio.sleep(interval)

    # ---------------------------------------------------------------- 查询

    def query(self, metric: str, points: int = 120, disk: str = "") -> dict:
        meta = METRICS.get(metric)
        if not meta:
            raise ValueError(f"未知指标：{metric}，可用：{'/'.join(METRICS)}")
        points = max(1, min(int(points), self._max))

        base = {
            "metric": metric,
            "unit": meta["unit"],
            "label": meta["label"],
            "interval": self._s.history_interval,
            "capacity": self._max,
            "last_sample_at": self._last_sample_at,
            "buffer_started_at": self._started_at,
            "stale": (
                self._last_sample_at is None
                or time.time() - self._last_sample_at > self._s.history_interval * 3
            ),
            "last_error": self._last_error,
        }

        if meta.get("per_disk"):
            source = self._sleep if metric == "disk_sleep" else self._disk
            if disk:
                series = source.get(disk)
                if series is None:
                    return {**base, "disk": disk, "since": None, "points": [],
                            "reason": f"没有磁盘 {disk} 的采样（盘名/标签需与 /storage 一致）"}
                return {**base, "disk": disk, "since": series[0][0],
                        "points": _window(series, points)}
            return {
                **base,
                "disks": {
                    k: {"since": v[0][0] if v else None, "points": _window(v, points)}
                    for k, v in source.items()
                },
            }

        series = self._series[metric]
        return {**base, "since": series[0][0] if series else None,
                "points": _window(series, points)}

    def estimate_net_bytes(self) -> dict:
        """用速率采样做梯形积分，估算「缓冲区起始以来」的网口累计流量。

        UGOS 不提供累计字节数，这里不假装是真实计数：`method` 明确标注为估算，
        且起点是缓冲区最早一个点（服务重启后清零）。
        """
        tx = list(self._series["net_tx"])
        rx = list(self._series["net_rx"])
        if len(tx) < 2 or len(rx) < 2:
            return {
                "supported": False,
                "reason": "历史采样点不足 2 个，无法积分",
                "samples": min(len(tx), len(rx)),
            }

        def _integrate(series: list[tuple[int, float]]) -> float:
            total = 0.0
            for (t0, v0), (t1, v1) in zip(series, series[1:]):
                dt = max(0, t1 - t0)
                total += (v0 + v1) / 2 * dt
            return total

        return {
            "supported": True,
            "rx_bytes": int(_integrate(rx)),
            "tx_bytes": int(_integrate(tx)),
            "since": min(tx[0][0], rx[0][0]),
            "until": max(tx[-1][0], rx[-1][0]),
            "samples": min(len(tx), len(rx)),
            "method": "rate_trapezoid",
            "note": "由本进程记录的速率采样积分估算，非 NAS 侧累计计数；起点为缓冲区最早采样点",
        }

    def metrics(self) -> dict:
        return {
            "since": self._started_at,
            "interval": self._s.history_interval,
            "capacity": self._max,
            "enabled": self._s.history_enabled,
            "samples": self._samples,
            "last_sample_at": self._last_sample_at,
            "last_error": self._last_error,
            "metrics": [
                {
                    "key": k,
                    "label": v["label"],
                    "unit": v["unit"],
                    "per_disk": bool(v.get("per_disk")),
                    "count": (
                        sum(len(s) for s in (
                            self._sleep if k == "disk_sleep" else self._disk
                        ).values())
                        if v.get("per_disk")
                        else len(self._series[k])
                    ),
                }
                for k, v in METRICS.items()
            ],
            "note": "历史只存在于内存，服务重启后清空，since 为当前缓冲最早时间",
        }


def _window(series: deque[tuple[int, float]], points: int) -> list[dict]:
    data = list(series)[-points:]
    return [{"ts": ts, "v": v} for ts, v in data]
