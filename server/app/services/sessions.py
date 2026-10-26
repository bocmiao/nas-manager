"""在线会话与登录审计（只读）。

探测结论（详见 docs/api-verified.md §六）：
- 在线会话：`POST v1/user/online/list`，body `{uid_list:[], user_type:"user", page, size}`，
  返回 `{total,page,size,list[{id,pid,login_time,platform,connect,protocol,host,ip,uid,
  username,login_status,is_current_connection,connection_duration}]}`
- 踢人：`POST v1/user/online/stop` **存在但未实现**（强制下线属高危操作，且只能踢 HTTP/HTTPS 会话）
- 登录审计没有独立接口，只能读审计日志 `v1/log/query?module=login`：
  成功 `[info] User [u] from [ip] logged in successfully.`
  失败 `[warning] User [u] from [ip] failed to log in due to incorrect username or password.`
  （失败形态用一次受控的错误密码登录实测得到，只试 1 次，远低于 NAS 自带 faillock 阈值）
- 来源 IP 是敏感信息：接口如实返回，**文档与示例一律用 192.0.2.10 占位**
"""
from __future__ import annotations

import logging
import re
import time

from ..cache import TTLCache
from ..config import Settings
from .logs import LogService
from .nas import NasService

log = logging.getLogger("sessions")

# 客户端类型（前端 platform 映射）
PLATFORM_LABELS = {
    2: "iOS", 4: "Android", 5: "-", 6: "TV", 7: "HarmonyOS",
    8: "HyperOS", 9: "PC/WEB", 10: "Car", 11: "HDMI",
}
# 协议（前端 protocol 映射）
PROTOCOL_LABELS = {0: "HTTP", 1: "HTTPS", 2: "SMB", 3: "FTP(S)", 4: "SFTP", 5: "SSH", 6: "-"}

# 登录日志正文：`User [<用户名>] from [<IP>] ...`
RE_USER_FROM_IP = re.compile(r"User\s+\[([^\]]+)\]\s+from\s+\[([^\]]+)\]")
RE_USER_ONLY = re.compile(r"User\s+\[?([^\]\s]+)\]?\s+logged")

FAIL_MARKERS = (
    "failed to log in", "log in failed", "login failed", "failed login",
    "incorrect password", "incorrect username", "authentication failure",
    "access denied", "login denied", "invalid password",
)
SUCCESS_MARKERS = ("logged in successfully", "logged into the system", "login successfully")


def parse_login_record(row: dict) -> dict:
    """把一条审计日志解析成登录事件。

    正文形态实测只有两种（成功 / 密码错误），但字段解析对未知文案做了兜底：
    解析不出就返回 `result="unknown"`，不猜成功。
    """
    content = str(row.get("content") or "")
    level = (row.get("level") or "").lower()
    m = RE_USER_FROM_IP.search(content)
    if m:
        username, ip = m.group(1), m.group(2)
    else:
        m2 = RE_USER_ONLY.search(content)
        username = m2.group(1) if m2 else None
        ip = None

    low = content.lower()
    if any(k in low for k in FAIL_MARKERS):
        result = "failed"
    elif any(k in low for k in SUCCESS_MARKERS):
        result = "success"
    elif level in ("warning", "critical"):
        # 文案没见过但级别是警告/严重，按失败处理（宁可多报一条"疑似失败"）
        result = "failed"
    else:
        result = "unknown"

    if "via ssh" in low:
        method = "ssh"
    elif "via smb" in low or "samba" in low:
        method = "smb"
    elif "via ftp" in low:
        method = "ftp"
    elif "web" in low or "from [" in content:
        method = "web"
    else:
        method = "unknown"

    return {
        **row,
        "username": username,
        "ip": ip,
        "result": result,
        "method": method,
    }


class SessionService:
    def __init__(self, nas: NasService, cache: TTLCache, settings: Settings,
                 logs: LogService | None = None) -> None:
        self._nas = nas
        self._cache = cache
        self._s = settings
        self._logs = logs or LogService(nas, cache, settings)

    @property
    def _c(self):
        return self._nas.client

    # ---------------------------------------------------------------- 在线会话

    async def online(self, page: int = 1, size: int = 50) -> dict:
        """当前在线会话。

        `ip` 是客户端来源地址，`host` 是客户端接入时用的 NAS 侧地址（同一批 Web 会话
        `host` 全都相同，SSH 会话两者一致）——语义按报文字段推断，前端只展示 `ip`。
        """

        async def _fetch() -> dict:
            raw = await self._c.post(
                "v1/user/online/list",
                {"uid_list": [], "user_type": "user", "page": max(1, page), "size": max(1, min(size, 200))},
            ) or {}
            rows = []
            for it in raw.get("list") or []:
                if not isinstance(it, dict):
                    continue
                platform = it.get("platform")
                protocol = it.get("protocol")
                rows.append(
                    {
                        "id": it.get("id"),
                        "username": it.get("username"),
                        "uid": it.get("uid"),
                        "ip": it.get("ip"),
                        "host": it.get("host"),
                        "login_at": it.get("login_time"),
                        "connection_seconds": it.get("connection_duration"),
                        "platform": platform,
                        "platform_label": PLATFORM_LABELS.get(platform, "未知"),
                        "protocol": protocol,
                        "protocol_label": PROTOCOL_LABELS.get(protocol, "未知"),
                        "connected": bool(it.get("connect")),
                        "login_status": bool(it.get("login_status")),
                        "is_current": bool(it.get("is_current_connection")),
                        "pid": it.get("pid") or None,
                    }
                )
            rows.sort(key=lambda x: (not x["is_current"], -(x["login_at"] or 0)))
            return {
                "total": raw.get("total", len(rows)),
                "page": raw.get("page", page),
                "size": raw.get("size", size),
                "items": rows,
                "by_user": _group_count(rows, "username"),
                "by_ip": _group_count(rows, "ip"),
                "by_protocol": _group_count(rows, "protocol_label"),
                "collected_at": int(time.time()),
            }

        return await self._cache.get_or_set(f"sessions:{page}:{size}", self._s.ttl_sessions, _fetch)

    async def online_summary(self) -> dict:
        """所有在线会话（跨页合并），供总览与告警用。"""
        first = await self.online(1, 200)
        total = first.get("total") or 0
        items = list(first.get("items") or [])
        page = 2
        while len(items) < total and page <= 10:
            nxt = await self.online(page, 200)
            chunk = nxt.get("items") or []
            if not chunk:
                break
            items.extend(chunk)
            page += 1
        return {"total": total, "items": items}

    # ---------------------------------------------------------------- 登录审计

    async def logins(
        self,
        page: int = 1,
        size: int = 50,
        only_failed: bool = False,
        user: str = "",
        since: int | None = None,
        until: int | None = None,
    ) -> dict:
        """登录审计日志。

        `only_failed=true` 时服务端按 `level=warning/critical` 过滤——
        实测密码错误的记录级别就是 warning，这样分页 total 与筛选结果一致。
        """
        levels = ["warning", "critical"] if only_failed else []
        data = await self._logs.query(
            page=page, size=size, levels=levels, module="login",
            keyword=user, since=since, until=until,
        )
        items = [parse_login_record(x) for x in data.get("items") or []]
        if user:
            items = [x for x in items if (x.get("username") or "").lower() == user.lower()
                     or user.lower() in str(x.get("content") or "").lower()]
        failed = sum(1 for x in items if x["result"] == "failed")
        return {
            **{k: v for k, v in data.items() if k != "items"},
            "only_failed": only_failed,
            "items": items,
            "page_failed": failed,
            "page_success": sum(1 for x in items if x["result"] == "success"),
            "note": "登录审计来自 v1/log/query?module=login；成功/失败由正文与级别判定，"
                    "解析不出的记录标 result=unknown，不计入失败数。",
        }

    # ---------------------------------------------------------------- 失败统计

    async def failures(self, window: int | None = None) -> dict:
        """窗口内按来源 IP 聚合的登录失败次数。

        巡检用它判断「同一 IP 短时间失败过多」。窗口是相对当前时间的滑动窗口，
        所以服务重启后第一条数据就是准的，不需要"首次建基线"。
        """
        win = max(60, int(window or self._s.alert_login_fail_window))
        now = int(time.time())
        rows = await self._logs.scan(
            levels=["warning", "critical"], module="login", since=now - win,
        )
        events = [parse_login_record(x) for x in rows]
        events = [e for e in events if e["result"] == "failed"]

        by_ip: dict[str, dict] = {}
        for e in events:
            ip = e.get("ip") or "unknown"
            node = by_ip.setdefault(
                ip, {"ip": ip, "count": 0, "users": set(), "first_at": None, "last_at": None,
                     "methods": set(), "samples": []},
            )
            node["count"] += 1
            if e.get("username"):
                node["users"].add(e["username"])
            node["methods"].add(e.get("method"))
            ts = e.get("create_time")
            if isinstance(ts, (int, float)):
                node["first_at"] = min(node["first_at"] or ts, ts)
                node["last_at"] = max(node["last_at"] or ts, ts)
            if len(node["samples"]) < 3:
                node["samples"].append(
                    {"at": ts, "username": e.get("username"), "content": e.get("content")}
                )

        items = []
        for node in by_ip.values():
            items.append(
                {
                    "ip": node["ip"],
                    "count": node["count"],
                    "users": sorted(node["users"]),
                    "methods": sorted(m for m in node["methods"] if m),
                    "first_at": node["first_at"],
                    "last_at": node["last_at"],
                    "samples": node["samples"],
                }
            )
        items.sort(key=lambda x: x["count"], reverse=True)
        return {
            "window_seconds": win,
            "since": now - win,
            "until": now,
            "scanned": len(rows),
            "failed_total": len(events),
            "items": items,
            "threshold": self._s.alert_login_fail_count,
            "over_threshold": [x for x in items if x["count"] >= self._s.alert_login_fail_count],
            "collected_at": now,
        }

    # ---------------------------------------------------------------- 防爆破配置

    async def faillock(self) -> dict:
        """NAS 自带的失败锁定策略与当前黑名单（只读，中间层不改这些配置）。"""

        async def _fetch() -> dict:
            cfg = await self._c.get("v1/security/faillock/config") or {}
            blocked: list = []
            try:
                raw = await self._c.get("v1/security/faillock/blocklist") or {}
                blocked = raw.get("result") or []
            except Exception as exc:  # noqa: BLE001 - 黑名单取不到不影响策略
                log.warning("读取 faillock 黑名单失败：%s", exc)
            inner = cfg.get("faillock") or {}
            unit_raw = cfg.get("unlock_unit") or "minute"
            unit = {"minute": "分钟", "second": "秒", "hour": "小时"}.get(
                str(unit_raw).lower(), str(unit_raw)
            )
            return {
                "enabled": bool(cfg.get("status")),
                "deny": inner.get("deny"),
                "fail_interval": inner.get("fail_interval"),
                "unlock_time": inner.get("unlock_time"),
                "unit": unit_raw,
                "blocked": blocked,
                "blocked_count": len(blocked),
                "threshold_hint": (
                    f"NAS 自带策略：{inner.get('fail_interval')} 分钟内失败 "
                    f"{inner.get('deny')} 次锁定 {inner.get('unlock_time')} {unit}；"
                    f"中间层告警阈值 ALERT_LOGIN_FAIL_COUNT 默认为 "
                    f"{self._s.alert_login_fail_count}，建议不高于 NAS 阈值才来得及提前预警"
                ),
                "read_only": True,
                "collected_at": int(time.time()),
            }

        return await self._cache.get_or_set("faillock", 60.0, _fetch)

    async def overview(self) -> dict:
        """会话 + 审计 + 防爆破的一屏汇总。"""
        online = await self.online_summary()
        fails = await self.failures()
        try:
            lock = await self.faillock()
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 faillock 失败：%s", exc)
            lock = {"enabled": None, "supported": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {
            "online": online,
            "failures": fails,
            "faillock": lock,
            "collected_at": int(time.time()),
        }


def _group_count(rows: list[dict], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        key = str(r.get(field) or "unknown")
        out[key] = out.get(key, 0) + 1
    return out
