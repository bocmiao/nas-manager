"""系统日志增强查询：分级 / 时间范围 / 关键词 / 分页。

实测契约（`GET v1/log/query`）：
- `page` **从 0 开始**（前端 `page: this.page - 1`），`size` 实测 500 仍可用
- `level` 只接受 `info` / `warning` / `critical`，传 `error` 直接 `8001 无效的输入参数`
- 同名参数重复即多选：`?level=warning&level=critical`
- `from_time` / `to_time` 为 Unix 秒；给一个未来区间会返回 0 条，说明确实按时间过滤
- 排序：`order=up|down` + `order_param=create_time|level|module|operator`
- 列表字段是 **`log_list`**，响应还带 `total` 与 `cur_page`（踩坑 31）
- 可选值从 `v1/log/usersandmodules` 取：`{levels, modules, users}`
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from ..cache import TTLCache
from ..config import Settings
from .nas import NasService

log = logging.getLogger("logs")

# 服务端只认这三个级别；对外额外接受 error/warn 作为别名，避免调用方踩 8001
LEVELS = ("info", "warning", "critical")
LEVEL_ALIASES = {"error": "critical", "err": "critical", "warn": "warning"}
LEVEL_ORDER = ("critical", "warning", "info")

# 单次扫描上限：巡检时只统计最近的失败记录，避免把整库日志翻一遍
SCAN_MAX_ROWS = 2000


def normalize_level(value: str) -> str:
    v = (value or "").strip().lower()
    v = LEVEL_ALIASES.get(v, v)
    if v not in LEVELS:
        raise ValueError(f"不支持的日志级别：{value}，可用：{'/'.join(LEVELS)}（error 会映射为 critical）")
    return v


def _iso(ts) -> str | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def normalize_item(it: dict) -> dict:
    ts = it.get("create_time")
    return {
        "id": it.get("log_id"),
        "level": it.get("level"),
        "module": it.get("module"),
        "operator": it.get("operator"),
        "content": it.get("content"),
        "create_time": ts,
        "time_iso": _iso(ts),
    }


class LogService:
    def __init__(self, nas: NasService, cache: TTLCache, settings: Settings) -> None:
        self._nas = nas
        self._cache = cache
        self._s = settings

    @property
    def _c(self):
        return self._nas.client

    # ---------------------------------------------------------------- 元信息

    async def meta(self) -> dict:
        """可选的级别 / 模块 / 操作者，供 App 端渲染筛选器。"""

        async def _fetch() -> dict:
            try:
                raw = await self._c.get("v1/log/usersandmodules") or {}
            except Exception as exc:  # noqa: BLE001 - 取不到就退回内置枚举
                log.warning("读取日志元信息失败：%s", exc)
                raw = {}
            return {
                "levels": raw.get("levels") or list(LEVELS),
                "modules": raw.get("modules") or [],
                "users": raw.get("users") or [],
                "level_aliases": LEVEL_ALIASES,
                "page_base": 0,
                "note": "NAS 侧 page 从 0 开始；中间层对外统一用 1 开始。"
                        "level 只接受 info/warning/critical。",
            }

        return await self._cache.get_or_set("log_meta", 300.0, _fetch)

    # ---------------------------------------------------------------- 查询

    async def query(
        self,
        page: int = 1,
        size: int = 50,
        levels: list[str] | tuple[str, ...] = (),
        module: str = "",
        keyword: str = "",
        since: int | None = None,
        until: int | None = None,
        order: str = "down",
        order_param: str = "create_time",
    ) -> dict:
        params: dict = {"page": max(0, int(page) - 1), "size": max(1, min(int(size), 500))}
        lv = [normalize_level(x) for x in levels if str(x).strip()]
        if lv:
            params["level"] = lv
        if module:
            params["module"] = module
        if keyword:
            params["keyword"] = keyword
        if since:
            params["from_time"] = int(since)
        if until:
            params["to_time"] = int(until)
        if order in ("up", "down"):
            params["order"] = order
            params["order_param"] = order_param

        raw = await self._c.get("v1/log/query", params) or {}
        rows = [normalize_item(x) for x in (raw.get("log_list") or []) if isinstance(x, dict)]
        total = raw.get("total")
        # cur_page 是 0 起的服务端回显，对外换算回 1 起
        cur = raw.get("cur_page")
        return {
            "page": (cur + 1) if isinstance(cur, int) else page,
            "size": params["size"],
            "total": total if isinstance(total, int) else len(rows),
            "pages": ((total + params["size"] - 1) // params["size"]) if isinstance(total, int) else None,
            "filters": {
                "levels": lv or None,
                "module": module or None,
                "keyword": keyword or None,
                "since": since,
                "until": until,
                "order": order,
                "order_param": order_param,
            },
            "items": rows,
            "collected_at": int(time.time()),
        }

    async def scan(
        self,
        levels: list[str] | tuple[str, ...] = (),
        module: str = "",
        keyword: str = "",
        since: int | None = None,
        until: int | None = None,
        max_rows: int = SCAN_MAX_ROWS,
        page_size: int = 200,
    ) -> list[dict]:
        """按页扫描，最多 max_rows 条。

        巡检只需要"窗口内某类日志有几条"，但 NAS 单页上限 500 且不保证一次取全，
        所以这里按页翻到凑够 max_rows 或翻完为止。
        """
        out: list[dict] = []
        page = 1
        while len(out) < max_rows:
            chunk = await self.query(
                page=page, size=page_size, levels=levels, module=module,
                keyword=keyword, since=since, until=until,
            )
            rows = chunk.get("items") or []
            if not rows:
                break
            out.extend(rows)
            total = chunk.get("total")
            if isinstance(total, int) and page * page_size >= min(total, max_rows):
                break
            page += 1
        return out[:max_rows]
