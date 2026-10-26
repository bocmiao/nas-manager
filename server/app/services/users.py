"""用户 / 用户组 / 共享权限（只读）。

探测来源：控制面板 `/ctlmgr` 的入口 bundle（`v1/user/*`、`v1/group/*`、`v2/user/permission`）。
**只做读**：创建/删除用户、改密码、改权限全部不实现（高危写操作）。

实测要点（详见 docs/api-verified.md）：
- `v1/user/list?reverse=false` 返回 `{"list":[...]}`，字段里**没有登录时间**
- 最后登录只能反查审计日志：`v1/log/query?module=login&keyword=<用户名>` 命中
  `content="User [xxx] from [...] logged in successfully."`、`operator=<用户名>`
- `v1/group/list` 在本机返回空数组：UGOS 只列自定义组，内置 `admin`(gid=10) 不出现，
  要靠用户的 `groups` 字段归纳
- `v1/user/list` 与 `v1/group/list` 带不带 `api-version: 2` 头结果一致
- 用户的 `otp_info.secret` 是敏感字段，一律不返回
"""
from __future__ import annotations

import logging
import time

from ..cache import TTLCache
from ..config import Settings
from .nas import NasService

log = logging.getLogger("users")

# 共享权限位：按 Unix rwx（4/2/1）解读，UGOS 未给出官方文档，故输出时标注为推断
PERM_BITS = (("read", 4), ("write", 2), ("execute", 1))


def _perm(mask) -> dict:
    try:
        m = int(mask)
    except (TypeError, ValueError):
        return {"mask": None, "read": None, "write": None, "execute": None}
    if m < 0:
        # 实测组权限里出现 -1，语义未知（不按位解析，避免误导）
        return {"mask": m, "read": None, "write": None, "execute": None,
                "note": "负值掩码语义未知，未做位解析"}
    return {
        "mask": m,
        "read": bool(m & 4),
        "write": bool(m & 2),
        "execute": bool(m & 1),
    }


def _rows(data) -> list[dict]:
    if isinstance(data, dict):
        for key in ("list", "result", "items", "data", "log_list"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


class UserService:
    def __init__(self, nas: NasService, cache: TTLCache, settings: Settings) -> None:
        self._nas = nas
        self._cache = cache
        self._s = settings

    @property
    def _c(self):
        return self._nas.client

    # ---------------------------------------------------------------- 用户

    async def users(self) -> dict:
        return await self._cache.get_or_set("users", self._s.ttl_users, self._fetch_users)

    async def _fetch_users(self) -> dict:
        raw = await self._c.get("v1/user/list", {"reverse": "false"})
        items = []
        for u in _rows(raw):
            uid = u.get("uid")
            username = u.get("username") or ""
            groups = [
                {"gid": g.get("gid"), "name": g.get("name") or ""}
                for g in (u.get("groups") or [])
                if isinstance(g, dict)
            ]
            role = (u.get("role") or "").lower()
            otp = u.get("otp_info") or {}
            last_login, login_source = await self._last_login(username)
            items.append(
                {
                    "uid": uid,
                    "username": username,
                    "display_name": u.get("fullname") or u.get("nickname") or username,
                    "role": role or None,
                    "is_admin": role == "admin" or any(g["name"] == "admin" for g in groups),
                    "account_type": u.get("account_type"),
                    "is_domain": bool(u.get("is_domain")),
                    "is_expired": bool(u.get("is_expired")),
                    "expire_at": u.get("expire_time") if u.get("expire_time", -1) > 0 else None,
                    "enable_home_dir": bool(u.get("enable_home_dir")),
                    "groups": groups,
                    "dir_quota": u.get("dir_quota") or 0,
                    "otp_enabled": bool(otp.get("status")),
                    "last_login_at": last_login,
                    "last_login_source": login_source,
                    "email": u.get("email") or None,
                    "phone": u.get("phone") or None,
                }
            )
        items.sort(key=lambda x: (not x["is_admin"], x["uid"] or 0))
        return {
            "total": len(items),
            "administrators": sum(1 for x in items if x["is_admin"]),
            "items": items,
            "read_only": True,
            "note": "只读。创建/删除用户、改密码、改权限属高危写操作，中间层不实现。",
            "collected_at": int(time.time()),
        }

    async def _last_login(self, username: str) -> tuple[int | None, str]:
        """反查审计日志里的最后登录时间。

        用户接口不返回登录时间，只能读审计日志。实测两种记录形态：
        - `module=login`、`operator=<用户名>`、`content="User [xxx] from [...] logged in successfully."`
          （Web / App 登录）
        - `module=control_panel`、`operator=system`、`content="User <用户名> logged into the system via SSH."`
          （SSH 登录，用户名只出现在 content 里）

        因此按用户名做一次 keyword 检索，优先取 operator 精确匹配，其次退到 content 命中。
        日志按时间倒序，取最新一条。
        """
        if not username:
            return None, "未提供用户名"
        key = f"lastlogin:{username}"
        hit = self._cache.peek(key)
        if hit and time.monotonic() - hit[0] < self._s.ttl_user_login:
            return hit[1]

        value: tuple[int | None, str] = (None, "审计日志中没有该用户的登录记录")
        try:
            raw = await self._c.get(
                "v1/log/query",
                {"page": 1, "size": 20, "keyword": username},
            )
            fallback = None
            for row in _rows(raw):
                ts = row.get("create_time")
                if not isinstance(ts, (int, float)) or ts <= 0:
                    continue
                if (row.get("operator") or "") == username:
                    value = (int(ts), "log:module=login")
                    break
                content = str(row.get("content") or "")
                low = content.lower()
                if fallback is None and username in content and (
                    "logged in" in low or "logged into" in low or "login" in low
                ):
                    # 兜底：SSH 登录（operator 是 system，用户名在正文里）
                    fallback = (int(ts), f"log:module={row.get('module') or 'unknown'}")
            else:
                if fallback:
                    value = fallback
        except Exception as exc:  # noqa: BLE001 - 日志不可用不影响用户列表
            log.warning("查询 %s 最后登录失败：%s", username, exc)
            value = (None, f"审计日志查询失败：{type(exc).__name__}")

        self._cache.put(key, value)
        return value

    async def detail(self, uid: int) -> dict:
        base = next((u for u in (await self.users())["items"] if u["uid"] == uid), None)
        if base is None:
            raise ValueError(f"未找到用户 uid={uid}")

        shares = []
        try:
            raw = await self._c.get(f"v1/user/permission/{uid}")
            for row in _rows(raw):
                shares.append(
                    {
                        "id": row.get("id"),
                        "name": row.get("name"),
                        "path": row.get("path"),
                        "type": row.get("type"),
                        "permission": row.get("permission"),
                        "permission_mask": row.get("permission_mask"),
                        "permissions": _perm(row.get("permission_mask")),
                        "time_backup": bool(row.get("time_backup")),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("读取用户 %s 共享权限失败：%s", uid, exc)

        apps = []
        try:
            raw = await self._c.get(f"v1/user/authorized/{uid}")
            for row in _rows(raw):
                apps.append({"app_id": row.get("id"), "allow": bool(row.get("allow"))})
        except Exception as exc:  # noqa: BLE001
            log.warning("读取用户 %s 应用授权失败：%s", uid, exc)

        quota = {}
        try:
            raw = await self._c.get(f"v1/user/quota/{uid}") or {}
            quota = {
                "home": raw.get("home") or {},
                "volumes": [
                    {
                        "path": v.get("path"),
                        "label": v.get("label"),
                        "used": v.get("used"),
                        "soft": v.get("soft"),
                        "hard": v.get("hard"),
                        "filesystem": v.get("file_system"),
                        "sub_dirs": v.get("sub") or [],
                        "max_value": v.get("max_value"),
                    }
                    for v in (raw.get("ext4") or []) + (raw.get("btrfs") or [])
                    if isinstance(v, dict)
                ],
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("读取用户 %s 配额失败：%s", uid, exc)

        return {
            **base,
            "shares": shares,
            "authorized_apps": apps,
            "quota": quota,
            "collected_at": int(time.time()),
        }

    # ---------------------------------------------------------------- 用户组

    async def groups(self) -> dict:
        return await self._cache.get_or_set("groups", self._s.ttl_users, self._fetch_groups)

    async def _fetch_groups(self) -> dict:
        custom = [
            {
                "gid": g.get("gid"),
                "name": g.get("name"),
                "description": g.get("description") or g.get("desc") or "",
                "builtin": False,
            }
            for g in _rows(await self._c.get("v1/group/list", {"reverse": "false"}))
        ]

        # 内置组不出现在 group/list，从用户的 groups 字段归纳
        observed: dict[int, dict] = {}
        for u in (await self.users())["items"]:
            for g in u["groups"]:
                gid = g["gid"]
                node = observed.setdefault(
                    gid, {"gid": gid, "name": g["name"], "builtin": True, "members": []}
                )
                if u["username"]:
                    node["members"].append(u["username"])

        for node in observed.values():
            try:
                perms = _rows(await self._c.get(f"v1/group/permission/{node['gid']}"))
                node["shares"] = [
                    {
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "path": p.get("path"),
                        "permission_mask": p.get("permission_mask"),
                        "permissions": _perm(p.get("permission_mask")),
                    }
                    for p in perms
                ]
            except Exception as exc:  # noqa: BLE001
                node["shares"] = []
                node["shares_error"] = f"{type(exc).__name__}: {exc}"

        items = list(observed.values()) + custom
        for node in items:
            node["member_count"] = len(node.get("members") or [])
        return {
            "total": len(items),
            "items": items,
            "note": "UGOS 只返回自定义用户组；内置 admin 组不在 group/list 中，"
                    "由用户所属组归纳得到（group/info 对内置组返回 9999 record not found）。",
            "collected_at": int(time.time()),
        }

    # ---------------------------------------------------------------- 共享文件夹

    async def shares(self) -> dict:
        return await self._cache.get_or_set("shares", self._s.ttl_users, self._fetch_shares)

    async def _fetch_shares(self) -> dict:
        raw = await self._c.get("v1/filemgr/getShareList")
        items = []
        for s in _rows(raw):
            items.append(
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "path": s.get("path"),
                    "type": s.get("type"),
                    "filesystem": s.get("filesystem"),
                    "permission_mask": s.get("permission_mask"),
                    "permissions": _perm(s.get("permission_mask")),
                    "time_backup": bool(s.get("time_backup")),
                    "is_mount": bool(s.get("is_mount")),
                    "owner": s.get("owner"),
                    "owner_type": s.get("owner_type"),
                    "size": s.get("size"),
                    "created_at": s.get("ctime"),
                    "modified_at": s.get("mtime"),
                }
            )
        return {
            "total": len(items),
            "items": items,
            "permission_note": "permission_mask 的位含义按 Unix rwx（4/2/1）推断，"
                               "UGOS 未提供官方文档；原始掩码一并返回。",
            "collected_at": int(time.time()),
        }
