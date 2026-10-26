"""同步与备份任务状态（只读）。

实测契约（详见 docs/api-verified.md §六）：
- 任务列表：`GET v1/web/sync/task/list`，返回 `{all_count, count, list:[]}`
- 查询参数会被当作**表字段过滤**：传未知字段名报 `9999 操作失败! no such column: xxx`，
  由此探得可用列：id / task_name / status / protocol / user / uid / progress / size /
  backup_type / sync_mode / backup_local_paths / backup_remote_paths / schedule_time
- 状态枚举（前端 TaskStatus，两套编号一致）：0=完成 1=失败 2=扫描中 3=已暂停
  4=进行中 5=未开始 6=等待；另有字符串形态 finished/partialSuccess/error/scanning/paused/progress/unStart
- 备份类型 FileBackupType：1=增量 2=镜像 3=多版本；计划类型 1=自动 2=手动 3=计划
- 执行历史：`GET v1/web/sync/log/task/list`；告警横幅：`GET v1/web/sync/warn/list`
- **本机没有配置任何备份任务**（list 为空），任务条目字段名按上表 + 前端归一化逻辑推断，
  因此 `schema_verified=false`，条目里同时给 `raw` 供客户端兜底

只读：创建/删除/立即执行任务（task/sync、task/delete、task/now）都不实现。
"""
from __future__ import annotations

import logging
import time

from ..cache import TTLCache
from ..config import Settings
from .nas import NasService

log = logging.getLogger("backup")

STATUS_LABELS = {
    0: ("finished", "已完成"),
    1: ("error", "失败"),
    2: ("scanning", "扫描中"),
    3: ("paused", "已暂停"),
    4: ("progress", "进行中"),
    5: ("unstart", "未开始"),
    6: ("waiting", "等待中"),
}
STATUS_FROM_TEXT = {
    "finished": 0, "partialsuccess": 0, "error": 1, "scanning": 2, "paused": 3,
    "progress": 4, "unstart": 5, "waiting": 6,
}
BACKUP_TYPES = {1: ("incremental", "增量"), 2: ("mirror", "镜像"), 3: ("multiversion", "多版本")}
SCHEDULE_TYPES = {1: ("auto", "自动"), 2: ("manual", "手动"), 3: ("plan", "计划")}

# 任务条目里可能出现的“上次/下次运行时间”字段名（列名探测没有得到，故做多键兜底）
LAST_KEYS = ("last_run_time", "last_run_at", "last_time", "last_exec_time",
             "last_backup_time", "last_finish_time", "end_time")
NEXT_KEYS = ("next_run_time", "next_run_at", "next_time", "next_exec_time",
             "next_backup_time", "schedule_time")


def _status_of(item: dict) -> tuple[int | None, str, str]:
    raw = item.get("status")
    if isinstance(raw, str):
        code = STATUS_FROM_TEXT.get(raw.strip().lower())
        if code is not None:
            return code, STATUS_LABELS[code][0], STATUS_LABELS[code][1]
        return None, raw, raw
    if isinstance(raw, bool):
        raw = int(raw)
    if isinstance(raw, int):
        key, label = STATUS_LABELS.get(raw, ("unknown", f"未知({raw})"))
        return raw, key, label
    return None, "unknown", "未知"


def _pick(item: dict, keys: tuple[str, ...]):
    for k in keys:
        v = item.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


def normalize_task(item: dict) -> dict:
    """把一条任务归一化。纯函数，便于用合成数据做回归（本机无真实任务）。"""
    code, key, label = _status_of(item)
    btype = item.get("backup_type")
    bkey, blabel = BACKUP_TYPES.get(btype, (None, None)) if isinstance(btype, int) else (None, None)
    stype = item.get("schedule_type")
    skey, slabel = SCHEDULE_TYPES.get(stype, (None, None)) if isinstance(stype, int) else (None, None)

    progress = item.get("progress")
    if isinstance(progress, float) and 0 <= progress <= 1:
        progress = round(progress * 100, 1)

    tid = item.get("id")
    return {
        "id": tid,
        "name": item.get("task_name") or item.get("name") or f"task-{tid}",
        "status_code": code,
        "status": key,
        "status_label": label,
        "running": code in (2, 4, 6),
        "failed": code == 1,
        "progress": progress,
        "protocol": item.get("protocol"),
        "user": item.get("user"),
        "uid": item.get("uid"),
        "size": item.get("size"),
        "backup_type": {"code": btype, "key": bkey, "label": blabel},
        "schedule_type": {"code": stype, "key": skey, "label": slabel},
        "sync_mode": item.get("sync_mode"),
        "local_paths": item.get("backup_local_paths") or item.get("local_paths") or [],
        "remote_paths": item.get("backup_remote_paths") or item.get("remote_paths") or [],
        "schedule_time": item.get("schedule_time"),
        "last_run_at": _pick(item, LAST_KEYS),
        "next_run_at": _pick(item, NEXT_KEYS),
        "raw": item,
    }


class BackupService:
    def __init__(self, nas: NasService, cache: TTLCache, settings: Settings) -> None:
        self._nas = nas
        self._cache = cache
        self._s = settings

    @property
    def _c(self):
        return self._nas.client

    async def tasks(self) -> dict:
        return await self._cache.get_or_set("backup_tasks", self._s.ttl_backup, self._fetch_tasks)

    async def _fetch_tasks(self) -> dict:
        raw = await self._c.get("v1/web/sync/task/list") or {}
        rows = [normalize_task(x) for x in (raw.get("list") or []) if isinstance(x, dict)]
        rows.sort(key=lambda x: (not x["failed"], -(x["last_run_at"] or 0)))

        last_known = max((r["last_run_at"] or 0 for r in rows), default=0) or None
        return {
            "total": len(rows),
            "all_count": raw.get("all_count"),
            "count": raw.get("count"),
            "items": rows,
            "running": sum(1 for r in rows if r["running"]),
            "failed": sum(1 for r in rows if r["failed"]),
            "last_run_known": last_known,
            # 本机 list 为空 → 字段名只能按前端归一化逻辑推断，不谎称已验证
            "schema_verified": bool(rows),
            "status_enum": {str(k): v[0] for k, v in STATUS_LABELS.items()},
            "read_only": True,
            "note": "只读。创建/删除/立即执行备份任务均不实现。",
            "collected_at": int(time.time()),
        }

    async def connections(self) -> dict:
        """已配对的同步客户端（如手机相册备份）。"""

        async def _fetch() -> dict:
            raw = await self._c.get("v1/web/sync/connection/list") or {}
            rows = [
                {
                    "connect_id": x.get("connect_id"),
                    "name": x.get("connect_name") or x.get("display_name"),
                    "user": x.get("user"),
                    "uid": x.get("uid"),
                    "protocol": x.get("sync_protocol"),
                    "state": x.get("connect_state"),
                    "note": x.get("note"),
                    "version": x.get("version") or None,
                }
                for x in (raw.get("result") or [])
                if isinstance(x, dict)
            ]
            return {"total": len(rows), "items": rows, "collected_at": int(time.time())}

        return await self._cache.get_or_set("backup_connections", 60.0, _fetch)

    async def runs(self, page: int = 1, size: int = 20) -> dict:
        """执行历史（`v1/web/sync/log/task/list`）。"""
        raw = await self._c.get("v1/web/sync/log/task/list", {"page": page, "size": size}) or {}
        rows = [x for x in (raw.get("list") or []) if isinstance(x, dict)]
        return {
            "total": raw.get("all_count", len(rows)),
            "page": page,
            "size": size,
            "items": rows,
            "schema_verified": bool(rows),
            "note": "本机没有任何执行记录（未配置任务），条目字段原样透出。",
            "collected_at": int(time.time()),
        }

    async def failures(self) -> dict:
        """失败任务 + NAS 侧的备份告警横幅。"""
        tasks = await self.tasks()
        failed = [t for t in (tasks.get("items") or []) if t.get("failed")]
        warn: dict = {}
        try:
            warn = await self._c.get("v1/web/sync/warn/list") or {}
        except Exception as exc:  # noqa: BLE001 - 横幅拿不到不影响失败列表
            log.warning("读取备份告警横幅失败：%s", exc)
            warn = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "failed_tasks": failed,
            "failed_total": len(failed),
            "nas_warnings": {
                "log_level": warn.get("log_level"),
                "items": warn.get("list") or [],
            },
            "collected_at": int(time.time()),
        }

    async def overview(self) -> dict:
        """任务 + 连接 + 执行摘要一屏。"""
        tasks = await self.tasks()
        try:
            conns = await self.connections()
        except Exception as exc:  # noqa: BLE001
            log.warning("读取同步客户端失败：%s", exc)
            conns = {"total": 0, "items": [], "error": f"{type(exc).__name__}: {exc}"}
        try:
            summary = await self._c.get("v1/web/sync/log/task/summary") or {}
        except Exception as exc:  # noqa: BLE001
            summary = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "tasks": tasks,
            "connections": conns,
            "transferring": (summary or {}).get("transferringCount"),
            "read_only": True,
            "collected_at": int(time.time()),
        }
