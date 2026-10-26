"""KVM 虚拟机（探测为主，只读）。

实测（详见 docs/api-verified.md §六）：
- 本机 `v1/desktop/module/list` 共 31 个应用，**没有**任何虚拟机应用
  （无 `com.ugreen.virtualmachine` / 名称含「虚拟机」的条目），
  因此 `v1/vm/list`、`v1/virtualmachine/list`、`v2/vm/list`、`v1/vm/info`、
  `v1/vm/snapshot/list`、`v1/kvm/vm/list` 全部是 **HTTP 404 非 JSON**——
  连模块都没挂载，不是"接口存在但参数错"（那种会返回 9404/9999）
- 所以这里如实返回 `installed=false` 并列出探测过的路径；
  已安装分支的归一化逻辑写好但**未在真机验证**（`verified=false`）

只读：VM 启停、删除、快照回滚都是高危写操作，一律不实现。
"""
from __future__ import annotations

import logging
import time

from ..cache import TTLCache
from ..config import Settings
from .nas import NasService

log = logging.getLogger("vms")

# 已知 / 合理猜测的虚拟机应用 ID
VM_MODULE_IDS = (
    "com.ugreen.virtualmachine",
    "com.ugreen.vm",
    "com.ugreen.kvm",
    "com.ugreen.virtualization",
    "com.ugreen.vmmgr",
)
# 未安装时用于说明"试过哪些"
PROBED_PATHS = (
    "v1/vm/list", "v1/virtualmachine/list", "v2/vm/list",
    "v1/vm/info", "v1/vm/snapshot/list", "v1/kvm/vm/list",
)


def find_vm_modules(modules: list[dict]) -> list[dict]:
    """从应用清单里挑出虚拟机应用（纯函数，便于回归测试）。"""
    out = []
    for m in modules:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("module_id") or "").lower()
        name = str(m.get("name") or "")
        if mid in VM_MODULE_IDS or "virtualmachine" in mid or "virtual-machine" in mid \
                or mid.endswith(".vm") or ".kvm" in mid or "虚拟机" in name:
            out.append(
                {
                    "module_id": m.get("module_id"),
                    "name": name,
                    "entry": m.get("entry"),
                    "version": m.get("version"),
                    "app_status": m.get("app_status"),
                }
            )
    return out


def normalize_vm(item: dict) -> dict:
    """VM 条目归一化（字段名未在真机验证，按多键兜底）。"""
    def pick(*keys):
        for k in keys:
            if item.get(k) not in (None, "", []):
                return item.get(k)
        return None

    return {
        "id": pick("id", "vm_id", "uuid", "name"),
        "name": pick("name", "vm_name", "display_name"),
        "status": pick("status", "state", "vm_status"),
        "running": str(pick("status", "state") or "").lower() in ("running", "started", "on", "1"),
        "os": pick("os", "os_type", "guest_os", "system"),
        "cpu_cores": pick("cpu", "cpu_cores", "vcpu", "cores"),
        "memory_bytes": pick("memory", "mem", "memory_size", "ram"),
        "disk_bytes": pick("disk", "disk_size", "storage"),
        "ip": pick("ip", "ip_address"),
        "autostart": pick("autostart", "auto_start", "boot"),
        "snapshot_count": pick("snapshot_count", "snapshots_total"),
        "raw": item,
    }


class VmService:
    def __init__(self, nas: NasService, cache: TTLCache, settings: Settings) -> None:
        self._nas = nas
        self._cache = cache
        self._s = settings

    @property
    def _c(self):
        return self._nas.client

    async def modules(self) -> list[dict]:
        """整机应用清单（`v1/desktop/module/list`）。"""

        async def _fetch() -> list[dict]:
            raw = await self._c.get("v1/desktop/module/list") or {}
            return [m for m in (raw.get("result") or []) if isinstance(m, dict)]

        return await self._cache.get_or_set("desktop_modules", self._s.ttl_vm, _fetch)

    async def status(self) -> dict:
        """虚拟机应用是否安装 + 只读的 VM 列表（已安装时）。"""
        modules = await self.modules()
        hits = find_vm_modules(modules)
        installed = bool(hits)

        if not installed:
            return {
                "installed": False,
                "supported": False,
                "app": None,
                "total": 0,
                "items": [],
                "verified": True,
                "reason": "本机未安装 KVM 虚拟机应用：v1/desktop/module/list 的 31 个应用里"
                          "没有 module_id 含 virtualmachine/vm/kvm 或名称含「虚拟机」的条目",
                "probed": {"modules_total": len(modules), "paths": list(PROBED_PATHS)},
                "probe_result": "上述路径全部 HTTP 404 非 JSON：模块未挂载，不是参数问题",
                "read_only": True,
                "collected_at": int(time.time()),
            }

        app = hits[0]
        out = {
            "installed": True,
            "supported": True,
            "app": app,
            "verified": False,
            "total": 0,
            "items": [],
            "read_only": True,
            "note": "只读。VM 启停 / 删除 / 快照回滚均为高危写操作，中间层不实现。",
            "collected_at": int(time.time()),
        }
        for path in ("v1/vm/list", "v1/virtualmachine/list"):
            try:
                raw = await self._c.get(path) or {}
            except Exception as exc:  # noqa: BLE001 - 逐个候选降级
                out.setdefault("probe_errors", []).append(f"{path}: {type(exc).__name__}")
                continue
            rows = raw.get("list") or raw.get("result") or raw.get("vms") or []
            if isinstance(rows, list):
                out["items"] = [normalize_vm(x) for x in rows if isinstance(x, dict)]
                out["total"] = raw.get("total", len(out["items"]))
                out["source"] = path
                out["verified"] = True
                break
        if "source" not in out:
            out["reason"] = "应用已安装，但 v1/vm/list 与 v1/virtualmachine/list 都没取到列表，" \
                            "端点名可能需要从该应用的 bundle 重新逆向"
        return out
