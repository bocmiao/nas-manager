#!/usr/bin/env python3
"""第四批功能探测脚本（会话 / 风扇 / 备份 / 电源 / 日志 / 虚拟机）。

用法:
    python scripts/probe_batch4.py session
    python scripts/probe_batch4.py fan
    python scripts/probe_batch4.py backup
    python scripts/probe_batch4.py power
    python scripts/probe_batch4.py logs
    python scripts/probe_batch4.py vm
    python scripts/probe_batch4.py all

判定依据：路由不存在 → HTTP 404 非 JSON；路由存在但参数/方法不对 → JSON + 9999/1005/9404/9405。
结果写到 .probe/batch4_<name>.json（已 gitignore，可含真实设备数据）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nas_login_test import load_env  # noqa: E402

from app.ugos.client import UgosClient, UgosError  # noqa: E402

PROBE_DIR = os.path.join(ROOT, ".probe")
os.makedirs(PROBE_DIR, exist_ok=True)


def dump(name: str, payload) -> None:
    path = os.path.join(PROBE_DIR, f"batch4_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n-> {path}")


async def call(c: UgosClient, method: str, path: str, **kw) -> dict:
    rec = {"method": method.upper(), "path": path,
           "args": kw.get("params") or kw.get("body")}
    try:
        if method == "get":
            data = await c.get(path, kw.get("params"), headers=kw.get("headers"))
        elif method == "post":
            data = await c.post(path, kw.get("body"), params=kw.get("params"),
                                headers=kw.get("headers"))
        else:
            data = await c.delete(path, kw.get("body"), params=kw.get("params"))
        rec["ok"] = True
        rec["data"] = data
    except UgosError as exc:
        rec["ok"] = False
        rec["code"] = exc.code
        rec["msg"] = exc.msg
        rec["debug"] = exc.debug
    except Exception as exc:  # noqa: BLE001
        rec["ok"] = False
        rec["error"] = f"{type(exc).__name__}: {exc}"
    tail = "" if rec.get("ok") else f"code={rec.get('code')} {rec.get('msg') or rec.get('error')}"
    print(f"  {'OK ' if rec.get('ok') else 'ERR'} {rec['method']:6s} {path} {tail}")
    return rec


def keys_of(data, depth: int = 0) -> str:
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            if isinstance(v, dict):
                parts.append(f"{k}{{{keys_of(v, depth + 1)}}}")
            elif isinstance(v, list):
                parts.append(f"{k}[{len(v)}]")
            else:
                parts.append(k)
        return ",".join(parts)
    return type(data).__name__


def first_item(data):
    if isinstance(data, dict):
        for k in ("list", "result", "items", "data", "log_list", "tasks"):
            v = data.get(k)
            if isinstance(v, list) and v:
                return v[0]
    if isinstance(data, list) and data:
        return data[0]
    return None


# ---------------------------------------------------------------- 会话 / 登录审计

async def probe_session(c: UgosClient) -> None:
    out = []
    # 在线会话：POST + body（前端 API 定义 userOnlineList(t) → post）
    out.append(await call(c, "post", "v1/user/online/list",
                          body={"uid_list": [], "user_type": "user", "page": 1, "size": 50}))
    out.append(await call(c, "post", "v1/user/online/list",
                          body={"page": 1, "size": 50}))
    out.append(await call(c, "get", "v1/user/online/list",
                          params={"page": 1, "size": 50}))
    # 防暴力破解配置（只读参考：NAS 自带的失败锁定策略）
    out.append(await call(c, "get", "v1/security/faillock/config"))
    out.append(await call(c, "get", "v1/security/faillock/blocklist"))
    out.append(await call(c, "get", "v1/security/faillock/whitelist"))
    # 审计日志：登录相关
    out.append(await call(c, "get", "v1/log/usersandmodules"))
    out.append(await call(c, "get", "v1/log/query",
                          params={"page": 0, "size": 5, "module": "login"}))
    # 猜测路径（会话 / 登录记录）
    for p in ("v1/user/login/record", "v1/user/login/list", "v1/user/session/list",
              "v1/session/list", "v1/log/login", "v1/user/online/history",
              "v1/security/login/record", "v1/security/login/list"):
        out.append(await call(c, "get", p))
    dump("session", out)

    rec = next((r for r in out if r.get("path") == "v1/user/online/list"
                and r.get("method") == "POST" and r.get("ok")), None)
    if rec:
        data = rec.get("data") or {}
        rows = data.get("list") or []
        print(f"\n  在线会话 {len(rows)} 条 / total={data.get('total')}")
        if rows:
            print("  字段：", keys_of(rows[0]))


# ---------------------------------------------------------------- 风扇

async def probe_fan(c: UgosClient) -> None:
    out = []
    out.append(await call(c, "get", "v1/hardware/fan/config"))
    out.append(await call(c, "get", "v1/hardware/cpu/config"))
    out.append(await call(c, "get", "v1/hardware/cpu/frequency"))
    out.append(await call(c, "get", "v1/taskmgr/stat/get_all"))
    # 只读探测其它风扇候选（不调用 fan/start 与 fan/setfan：写操作会改硬件策略）
    for p in ("v1/hardware/fan/status", "v1/hardware/fan/list",
              "v1/hardware/fan/speed", "v1/hardware/temperature",
              "v1/hardware/sensor/list", "v1/sysinfo/fan"):
        out.append(await call(c, "get", p))
    dump("fan", out)

    cfg = next((r for r in out if r.get("path") == "v1/hardware/fan/config"
                and r.get("ok")), None)
    if cfg:
        print("\n  fan/config 顶层字段：", keys_of(cfg.get("data") or {}))


# ---------------------------------------------------------------- 备份 / 同步

async def probe_backup(c: UgosClient) -> None:
    out = []
    out.append(await call(c, "get", "v1/web/sync/task/list"))
    out.append(await call(c, "get", "v1/web/sync/task/list", params={"page": 1, "size": 50}))
    out.append(await call(c, "get", "v1/web/sync/connection/list"))
    out.append(await call(c, "get", "v1/web/sync/task/policy/list"))
    out.append(await call(c, "get", "v1/web/sync/log/task/list", params={"page": 1, "size": 20}))
    out.append(await call(c, "get", "v1/web/sync/log/task/summary"))
    out.append(await call(c, "get", "v1/web/sync/warn/list"))
    out.append(await call(c, "get", "v1/web/sync/log/transfer/search", params={"page": 1, "size": 5}))
    out.append(await call(c, "get", "v1/web/sync/general/get"))
    dump("backup", out)

    hits = [r for r in out if r.get("path") == "v1/web/sync/task/list" and r.get("ok")]
    tl = next((r for r in hits if r.get("args") == {"page": 1, "size": 50}),
              hits[0] if hits else None)
    print("")
    if tl:
        data = tl.get("data") or {}
        rows = data.get("list") or []
        print(f"  任务 {len(rows)} 条 / total={data.get('total')}")
        if rows:
            print("  字段：", keys_of(rows[0]))
            print("  首条：", json.dumps(rows[0], ensure_ascii=False)[:1200])


# ---------------------------------------------------------------- 电源计划

async def probe_power(c: UgosClient) -> None:
    out = []
    out.append(await call(c, "get", "v1/hardware/power/config"))
    for p in ("v1/hardware/power/plan", "v1/hardware/power/schedule",
              "v1/hardware/power/list", "v1/power/plan", "v1/power/list",
              "v1/hardware/power/time_switch", "v1/hardware/wol/config"):
        out.append(await call(c, "get", p))
    dump("power", out)

    cfg = next((r for r in out if r.get("path") == "v1/hardware/power/config"
                and r.get("ok")), None)
    if cfg:
        data = cfg.get("data") or {}
        print("\n  power/config 顶层字段：", keys_of(data))
        for k in ("time_switches", "power_boot", "wake_on", "enable_scheduled_power"):
            if k in data:
                print(f"  {k} = {json.dumps(data[k], ensure_ascii=False)[:600]}")


# ---------------------------------------------------------------- 日志

async def probe_logs(c: UgosClient) -> None:
    out = []
    out.append(await call(c, "get", "v1/log/query", params={"page": 0, "size": 5}))
    out.append(await call(c, "get", "v1/log/query", params={"page": 1, "size": 5}))
    out.append(await call(c, "get", "v1/log/query", params={"page": 0, "size": 5, "level": "error"}))
    out.append(await call(c, "get", "v1/log/query", params={"page": 0, "size": 5, "level": 3}))
    out.append(await call(c, "get", "v1/log/query",
                          params={"page": 0, "size": 5, "module": "login"}))
    out.append(await call(c, "get", "v1/log/query", params={"page": 0, "size": 5, "keyword": "login"}))
    out.append(await call(c, "get", "v1/log/query",
                          params={"page": 0, "size": 5, "from_time": 1, "to_time": 2}))
    out.append(await call(c, "get", "v1/log/query", params={"page": 0, "size": 5, "order": "down",
                                                            "order_param": "create_time"}))
    out.append(await call(c, "get", "v1/log/usersandmodules"))
    dump("logs", out)

    base = next((r for r in out if r.get("args") == {"page": 0, "size": 5} and r.get("ok")), None)
    if base:
        data = base.get("data") or {}
        rows = data.get("log_list") or []
        print(f"\n  page=0 → {len(rows)} 条, total={data.get('total')}, cur_page={data.get('cur_page')}")
        print("  字段：", keys_of(rows[0]) if rows else "空")
        # 统计 level / module 取值分布
        lv: dict = {}
        md: dict = {}
        for r in rows:
            lv[r.get("level")] = lv.get(r.get("level"), 0) + 1
            md[r.get("module")] = md.get(r.get("module"), 0) + 1
        print("  level 取值：", lv)
        print("  module 取值：", md)


# ---------------------------------------------------------------- 虚拟机

VM_IDS = ("com.ugreen.virtualmachine", "com.ugreen.vm", "com.ugreen.kvm",
          "com.ugreen.virtualization", "com.ugreen.vmmgr")


async def probe_vm(c: UgosClient) -> None:
    out = []
    mods_rec = await call(c, "get", "v1/desktop/module/list")
    out.append(dict(mods_rec, data=None))
    mods = ((mods_rec.get("data") or {}).get("result") or []) if mods_rec.get("ok") else []
    ids = [m.get("module_id") for m in mods if isinstance(m, dict)]
    print(f"  已安装应用 {len(ids)} 个")
    hits = [m for m in mods if isinstance(m, dict)
            and ("virtual" in str(m.get("module_id", "")).lower()
                 or "kvm" in str(m.get("module_id", "")).lower()
                 or "虚拟机" in str(m.get("name", "")))]
    print(f"  虚拟机应用命中：{[h.get('module_id') for h in hits] or '无'}")

    # 即便未安装，也确认一下接口是否存在于后端
    for p in ("v1/vm/list", "v1/virtualmachine/list", "v2/vm/list", "v1/vm/info",
              "v1/vm/snapshot/list", "v1/kvm/vm/list"):
        out.append(await call(c, "get", p))
    dump("vm", out)


# ---------------------------------------------------------------- main

async def main() -> int:
    env = load_env()
    base = f"https://{env.get('UGOS_HOST', '192.0.2.10')}:{env.get('UGOS_PORT', '9443')}"
    c = UgosClient(base, env.get("UGOS_USER", ""), env.get("UGOS_PASSWORD", ""))
    names = sys.argv[1:] or ["session"]
    if names == ["all"]:
        names = ["session", "fan", "backup", "power", "logs", "vm"]
    try:
        await c.login()
        for name in names:
            print(f"\n=== {name}")
            fn = globals().get(f"probe_{name}")
            if not fn:
                print(f"未知探针：{name}")
                continue
            await fn(c)
    finally:
        await c.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
