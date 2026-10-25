"""快照（UGOS「快照」应用，`com.ugreen.snapshot`）。

端点来源：从 `/snapshot` 应用的 SPA bundle 里挖出来（storagemgr 里**没有**快照接口，
只有 `snapshot_size` 这种空间占用字段），再逐个实测确认：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `v1/snapshot/folder/{folder_type}` | 快照文件夹列表，`page_number` / `page_size` 必传 |
| GET/POST | `v1/snapshot/folder/{folder_type}/settings/{folder_id}` | 快照计划 + 保留策略 |
| GET | `v1/snapshot/snapshot` | 某文件夹的快照列表（同样 `page_number`/`page_size`） |
| POST | `v1/snapshot/snapshot` | 创建快照，body `{folder_type, folder_id}` |
| DELETE | `v1/snapshot/snapshot` | 删除快照，body 里带快照名 |
| POST | `v1/snapshot/snapshot/restore` | 回滚，body 里带快照名 |

`folder_type` 只接受 `share`（共享文件夹）和 `home`（用户家目录）。

本机实测结论（volume1 = ext4）：
- 创建 / 回滚 → `9999` + `folderServ.GetBaseFolder(): mnt point of home is not btrfs filesystem`
- 列表 → `32003 文件夹不存在`（没有任何文件夹被纳入快照管理）
- 删除 → 恒返回 `success`（对不存在的快照也一样，幂等且无法据此判断真伪）
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..cache import TTLCache
from ..config import Settings
from ..ugos.client import UgosError
from .nas import NasService

log = logging.getLogger("snapshots")

FOLDER_TYPES = ("share", "home")
BTRFS = "btrfs"

# 删除/回滚请求体里的字段名（服务端未回显校验细节，属推断值）
_NAME_FIELD_DELETE = "snapshots"
_NAME_FIELD_RESTORE = "snapshot"


def explain_error(exc: UgosError) -> dict:
    """把 UGOS 错误翻成客户端能直接展示的结论。"""
    dbg = exc.debug or ""
    low = dbg.lower()
    if "not btrfs" in low:
        reason = "目标文件系统不是 Btrfs，UGOS 快照仅支持 Btrfs 卷/文件夹"
        code = "unsupported_filesystem"
    elif exc.code == 32003:
        reason = "快照文件夹不存在：该共享文件夹/家目录未纳入快照管理"
        code = "folder_not_found"
    elif exc.code == 1300:
        reason = "无访问权限（快照应用要求文件夹已被快照服务接管）"
        code = "forbidden"
    elif exc.code == 9999 and "limit" in low:
        reason = "分页参数缺失（该接口强制要求 page_number/page_size）"
        code = "bad_params"
    elif exc.code == 9999 and "unknown folder type" in low:
        reason = "folder_type 非法，只支持 share / home"
        code = "bad_folder_type"
    else:
        reason = exc.msg or f"UGOS 返回 {exc.code}"
        code = f"ugos_{exc.code}"
    return {
        "supported": False,
        "reason": reason,
        "error_code": code,
        "ugos_code": exc.code,
        "ugos_debug": dbg or None,
    }


def _rows(data: Any) -> list[dict]:
    """快照列表的返回结构未定型，按常见键逐个尝试。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("snapshots", "snapshot_list", "list", "result", "items", "data"):
        val = data.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    return []


def _norm_snapshot(x: dict) -> dict:
    """字段名未经真实数据验证，尽量保留原始对象供客户端回退。"""
    return {
        "name": x.get("name") or x.get("snapshot_name") or x.get("snapshot"),
        "id": x.get("id") or x.get("snapshot_id"),
        "folder_id": x.get("folder_id"),
        "volume": x.get("volume") or x.get("volume_name") or x.get("pool"),
        "created_at": x.get("create_time") or x.get("created_at") or x.get("ctime"),
        "size": x.get("size") or x.get("used") or x.get("snapshot_size"),
        "remark": x.get("comment") or x.get("remark") or x.get("description"),
        "raw": x,
    }


class SnapshotService:
    def __init__(self, nas: NasService, cache: TTLCache, settings: Settings) -> None:
        self._nas = nas
        self._cache = cache
        self._s = settings

    # ---------------------------------------------------------------- 能力

    async def _filesystems(self) -> dict:
        """卷文件系统清单：UGOS 快照只支持 Btrfs。"""
        vols = (await self._nas.storage()).get("volumes") or []
        return {
            "volumes": [
                {"name": v.get("name"), "filesystem": v.get("filesystem"), "mount": v.get("mount")}
                for v in vols
            ],
            "has_btrfs": any((v.get("filesystem") or "").lower() == BTRFS for v in vols),
        }

    async def overview(self) -> dict:
        fs = await self._filesystems()
        folders: dict[str, dict] = {}
        for ftype in FOLDER_TYPES:
            folders[ftype] = await self.folders(ftype)

        total = sum(f.get("total", 0) for f in folders.values())
        supported = fs["has_btrfs"] and total > 0
        if supported:
            reason = None
        elif not fs["has_btrfs"]:
            reason = "没有 Btrfs 卷：UGOS 快照仅支持 Btrfs 文件系统，当前卷为 " + ", ".join(
                f"{v['name']}={v['filesystem']}" for v in fs["volumes"]
            )
        else:
            reason = "快照服务可用，但没有任何文件夹启用快照"

        return {
            "supported": supported,
            "reason": reason,
            "folder_total": total,
            "folders": {k: v.get("items") or [] for k, v in folders.items()},
            "folders_meta": {k: {kk: vv for kk, vv in v.items() if kk != "items"}
                             for k, v in folders.items()},
            "filesystems": fs,
            "note": "创建/删除/回滚均需 Btrfs；非 Btrfs 卷上 UGOS 直接拒绝",
            "collected_at": int(time.time()),
        }

    # ---------------------------------------------------------------- 文件夹

    async def folders(self, folder_type: str) -> dict:
        self._check_type(folder_type)

        async def _fetch() -> dict:
            try:
                data = await self._nas.client.get(
                    f"v1/snapshot/folder/{folder_type}",
                    {"page_number": 1, "page_size": 200},
                ) or {}
            except UgosError as exc:
                return {"folder_type": folder_type, "total": 0, "items": [], **explain_error(exc)}

            items = [x for x in (data.get("folders") or []) if isinstance(x, dict)]
            return {
                "supported": True,
                "folder_type": folder_type,
                "total": int(data.get("total_number") or len(items)),
                "items": [
                    {
                        "id": x.get("id") or x.get("folder_id"),
                        "name": x.get("name") or x.get("folder_name"),
                        "path": x.get("path"),
                        "remark": x.get("comment") or x.get("remark"),
                        "snapshot_count": x.get("snapshot_number") or x.get("count"),
                        "created_at": x.get("create_time") or x.get("ctime"),
                        "raw": x,
                    }
                    for x in items
                ],
            }

        return await self._cache.get_or_set(f"snap_folders:{folder_type}", 30.0, _fetch)

    async def schedule(self, folder_type: str, folder_id: int) -> dict:
        """快照计划与保留策略（只读）。"""
        self._check_type(folder_type)
        try:
            data = await self._nas.client.get(
                f"v1/snapshot/folder/{folder_type}/settings/{folder_id}"
            ) or {}
        except UgosError as exc:
            return {"folder_type": folder_type, "folder_id": folder_id, **explain_error(exc)}
        return {
            "supported": True,
            "folder_type": folder_type,
            "folder_id": folder_id,
            "schedule": data.get("snapshot_schedule") or {},
            "retention": data.get("retention_policy") or {},
            "note": "本机对任意 folder_id 都返回同一份默认值，说明该接口不做存在性校验",
        }

    # ---------------------------------------------------------------- 快照

    async def snapshots(self, folder_type: str, folder_id: int,
                        page: int = 1, size: int = 50) -> dict:
        self._check_type(folder_type)
        try:
            data = await self._nas.client.get(
                "v1/snapshot/snapshot",
                {
                    "folder_type": folder_type,
                    "folder_id": folder_id,
                    "page_number": page,
                    "page_size": size,
                },
            ) or {}
        except UgosError as exc:
            return {
                "folder_type": folder_type,
                "folder_id": folder_id,
                "items": [],
                **explain_error(exc),
            }

        rows = _rows(data)
        return {
            "supported": True,
            "folder_type": folder_type,
            "folder_id": folder_id,
            "page": page,
            "total": data.get("total_number") or data.get("total") or len(rows),
            "items": [_norm_snapshot(x) for x in rows],
            "raw": None if rows else data,
            "collected_at": int(time.time()),
        }

    async def create(self, folder_type: str, folder_id: int, comment: str = "") -> dict:
        """创建快照（写操作）。`comment` 为备注，字段名未验证。"""
        self._check_type(folder_type)
        body: dict[str, Any] = {"folder_type": folder_type, "folder_id": folder_id}
        if comment:
            body["comment"] = comment
        try:
            result = await self._nas.client.post("v1/snapshot/snapshot", body)
        except UgosError as exc:
            return {"action": "create", "ok": False, "body": body, **explain_error(exc)}
        self._cache.invalidate("snap_folders")
        return {
            "action": "create",
            "ok": True,
            "folder_type": folder_type,
            "folder_id": folder_id,
            "comment": comment or None,
            "server_result": result,
        }

    async def delete(self, folder_type: str, folder_id: int, names: list[str]) -> dict:
        """删除快照（写操作，可批量）。

        注意：该端点对不存在的快照也返回 200，因此这里会重新列一次做校验，
        列表本身不可用时如实返回 `verified: false`。
        """
        self._check_type(folder_type)
        names = [n for n in (names or []) if n]
        if not names:
            raise ValueError("必须指定要删除的快照名")

        body = {
            "folder_type": folder_type,
            "folder_id": folder_id,
            _NAME_FIELD_DELETE: names,
        }
        try:
            result = await self._nas.client.delete("v1/snapshot/snapshot", body)
        except UgosError as exc:
            return {"action": "delete", "ok": False, "body": body, **explain_error(exc)}

        verified: bool | None = None
        remaining: list[str] = []
        after = await self.snapshots(folder_type, folder_id, 1, 200)
        if after.get("supported"):
            remaining = [x.get("name") for x in after.get("items") or [] if x.get("name")]
            verified = not any(n in remaining for n in names)

        self._cache.invalidate("snap_folders")
        return {
            "action": "delete",
            "ok": True,
            "folder_type": folder_type,
            "folder_id": folder_id,
            "names": names,
            "verified": verified,
            "remaining": remaining or None,
            "server_result": result,
            "note": "UGOS 对不存在的快照同样返回 success；verified=false 表示无法确认是否真的删掉",
        }

    async def restore(self, folder_type: str, folder_id: int, name: str,
                      confirm: str) -> dict:
        """回滚到指定快照。

        回滚会覆盖当前数据且不可撤销，必须显式传 `confirm=ROLLBACK`，
        默认调用一律拒绝。
        """
        self._check_type(folder_type)
        if confirm != "ROLLBACK":
            raise ValueError("回滚是危险操作，需显式传 confirm=ROLLBACK 才执行")
        if not name:
            raise ValueError("必须指定要回滚到的快照名")

        body = {
            "folder_type": folder_type,
            "folder_id": folder_id,
            _NAME_FIELD_RESTORE: name,
        }
        try:
            result = await self._nas.client.post("v1/snapshot/snapshot/restore", body)
        except UgosError as exc:
            return {"action": "restore", "ok": False, "body": body, **explain_error(exc)}
        self._cache.invalidate("snap_folders")
        return {
            "action": "restore",
            "ok": True,
            "folder_type": folder_type,
            "folder_id": folder_id,
            "name": name,
            "server_result": result,
            "warning": "回滚不可撤销，请确认 NAS 侧已完成",
        }

    @staticmethod
    def _check_type(folder_type: str) -> None:
        if folder_type not in FOLDER_TYPES:
            raise ValueError(f"folder_type 只支持 {'/'.join(FOLDER_TYPES)}")
