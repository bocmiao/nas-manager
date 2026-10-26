#!/usr/bin/env python3
"""第三批功能探测脚本。

用法:
    python scripts/probe_batch3.py users      # 用户 / 用户组 / 共享权限
    python scripts/probe_batch3.py sleep      # 硬盘休眠
    python scripts/probe_batch3.py net        # 网口详情
    python scripts/probe_batch3.py storage    # 存储健康（inode / 校验）
    python scripts/probe_batch3.py services   # 系统服务清单

结果写到 .probe/batch3_<name>.json（已 gitignore，可含真实设备数据）。
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

# apiVersion=2 是前端请求选项，实测需要落到请求头（见探测结果）
API2 = {"api-version": "2"}


def dump(name: str, payload) -> None:
    path = os.path.join(PROBE_DIR, f"batch3_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n-> {path}")


async def try_call(c: UgosClient, method: str, path: str, **kw) -> dict:
    rec = {"method": method.upper(), "path": path, "params": kw.get("params") or kw.get("body")}
    try:
        if method == "get":
            data = await c.get(path, kw.get("params"), headers=kw.get("headers"))
        else:
            data = await c.post(path, kw.get("body"), params=kw.get("params"),
                                headers=kw.get("headers"))
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
    print(f"  {'OK ' if rec.get('ok') else 'ERR'} {rec['method']:5s} {path} {tail}")
    return rec


def brief(data, max_list: int = 2):
    if isinstance(data, dict):
        return {k: brief(v, max_list) for k, v in list(data.items())[:40]}
    if isinstance(data, list):
        return [brief(x, max_list) for x in data[:max_list]]
    return data


# ---------------------------------------------------------------- 用户

async def probe_users(c: UgosClient) -> None:
    out = []
    out.append(await try_call(c, "get", "v1/user/list", params={"reverse": "false"}))
    out.append(await try_call(c, "get", "v1/user/list", params={"reverse": "false"},
                              headers=API2))
    out.append(await try_call(c, "get", "v1/group/list", params={"reverse": "false"}))
    out.append(await try_call(c, "get", "v1/group/list", params={"reverse": "false"},
                              headers=API2))

    # 单用户详情 / 权限 / 配额，uid 从上面列表里取
    users = _list_of(out[1]) or _list_of(out[0])
    if users:
        uid = users[0].get("uid")
        print(f"\n  用 uid={uid} 探测详情")
        out.append(await try_call(c, "get", f"v1/user/uid/{uid}", headers=API2))
        out.append(await try_call(c, "get", f"v1/user/permission/{uid}"))
        out.append(await try_call(c, "get", f"v1/user/authorized/{uid}"))
        out.append(await try_call(c, "get", f"v1/user/quota/{uid}"))
        out.append(await try_call(c, "post", "v2/user/permission", body={"uids": [uid]}))
        out.append(await try_call(c, "post", "v2/user/permission", body={"uid": uid}))

    groups = _list_of(out[3]) or _list_of(out[2])
    if groups:
        gid = groups[0].get("gid") or groups[0].get("id")
        print(f"\n  用 gid={gid} 探测用户组权限")
        out.append(await try_call(c, "get", f"v1/group/info/{gid}"))
        out.append(await try_call(c, "get", f"v1/group/permission/{gid}"))
        out.append(await try_call(c, "get", f"v1/group/authorized/{gid}"))
        out.append(await try_call(c, "post", "v2/group/permission", body={"gids": [gid]}))
        out.append(await try_call(c, "post", "v1/group/gids/permission", body={"gids": [gid]}))

    out.append(await try_call(c, "get", "v1/filemgr/getShareList"))
    out.append(await try_call(c, "get", "v2/user/directory/quota"))
    out.append(await try_call(c, "get", "v1/user/config/global"))
    out.append(await try_call(c, "get", "v1/user/config/password"))
    out.append(await try_call(c, "get", "v1/user/check/quota"))
    dump("users", [dict(r, data=brief(r.get("data"), 1)) for r in out])
    # 完整原始用户列表另存，便于字段核对
    if len(out) > 1 and out[1].get("ok"):
        dump("users_raw", out[1]["data"])


def _list_of(rec: dict) -> list:
    data = rec.get("data")
    if isinstance(data, dict):
        for k in ("list", "result", "users", "items"):
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


# ---------------------------------------------------------------- 休眠

SLEEP_PATHS = [
    "v1/storage/disk/setting/get",
    "v1/storage/disk/vars",
    "v1/storage/disk/detection/list",
    "v1/storage/disk/cron/list",
    "v1/hardware/power/config",
    "v1/storage/disk/sleep",
    "v1/storage/disk/standby",
    "v1/storage/disk/hibernate",
    "v1/storage/disk/power",
    "v1/storage/disk/power/state",
    "v1/storage/disk/status",
    "v1/power/disk/list",
    "v1/power/config",
    "v1/hardware/disk/power",
    "v1/taskmgr/stat/disk",
]


async def probe_sleep(c: UgosClient) -> None:
    out = []
    for p in SLEEP_PATHS:
        out.append(await try_call(c, "get", p))
    # 磁盘列表里已有的 is_standby 字段 + 每盘 runningState
    lst = await try_call(c, "get", "v2/storage/disk/list")
    out.append(dict(lst, data=brief(lst.get("data"), 1)))
    disks = _list_of(lst)
    for d in disks[:2]:
        dev = f"/dev/{d.get('name')}"
        out.append(await try_call(c, "get", "v1/storage/disk/runningState",
                                  params={"disk": dev}))
        out.append(await try_call(c, "get", "v1/storage/disk/attribute",
                                  params={"disk": dev}))
    if disks:
        print("\n  磁盘条目字段：", list(disks[0].keys()))
        print("  首盘 is_standby/status =", disks[0].get("is_standby"), disks[0].get("status"))
    dump("sleep", out)


# ---------------------------------------------------------------- 网口

NET_PATHS = [
    "v1/network/interface/list",
    "v1/network/list",
    "v1/network/info",
    "v1/network/status",
    "v1/network/nic/list",
    "v1/network/ethernet/list",
    "v1/network/stat",
    "v1/sysinfo/network",
    "v1/sysinfo/net",
    "v2/sysinfo/network",
    "v1/taskmgr/stat/net",
    "v1/hardware/net/list",
    "v1/network/bond/list",
    "v1/network/config",
]


async def probe_net(c: UgosClient) -> None:
    out = []
    for p in NET_PATHS:
        out.append(await try_call(c, "get", p))
    # 硬件网口信息（已知可用）与实时网络统计
    out.append(await try_call(c, "get", "v1/sysinfo/machine/common"))
    out.append(await try_call(c, "get", "v1/taskmgr/stat/net"))
    dump("net", out)


# ---------------------------------------------------------------- 存储健康

HEALTH_PATHS = [
    "v1/storage/pool/extendInfo",
    "v1/storage/pool/capacity",
    "v1/storage/pool/describe",
    "v1/storage/pool/setting/get",
    "v1/storage/volume/describe",
    "v1/storage/volume/setting",
    "v1/storage/volume/list",
    "v1/storage/overview",
    "v1/storage/disk/detection/list",
]


async def probe_storage(c: UgosClient) -> None:
    out = []
    for p in HEALTH_PATHS:
        out.append(await try_call(c, "get", p))
    # 池 / 卷详情按名字再取一次（部分接口要参数）
    pools = _list_of(await try_call(c, "get", "v1/storage/pool/list"))
    if pools:
        name = pools[0].get("name")
        for p, params in (
            ("v1/storage/pool/extendInfo", {"pool": name}),
            ("v1/storage/pool/describe", {"name": name}),
            ("v1/storage/pool/capacity", {"name": name}),
        ):
            out.append(await try_call(c, "get", p, params=params))
    vols = _list_of(await try_call(c, "get", "v1/storage/volume/list"))
    if vols:
        name = vols[0].get("name")
        for p, params in (
            ("v1/storage/volume/describe", {"name": name}),
            ("v1/storage/volume/setting", {"name": name}),
        ):
            out.append(await try_call(c, "get", p, params=params))
    dump("storage", [dict(r, data=brief(r.get("data"), 2)) for r in out])
    if out and vols:
        dump("storage_volume_raw", {"volumes": vols, "pools": pools})


# ---------------------------------------------------------------- 服务

async def probe_services(c: UgosClient) -> None:
    rec = await try_call(c, "get", "v1/taskmgr/services")
    dump("services", rec)
    data = rec.get("data") or {}
    items = data.get("list") or []
    print(f"\n  服务数：{len(items)}")
    for s in items:
        if isinstance(s, dict):
            print(f"    id={s.get('id')} appid={s.get('appid')} name={s.get('name')}")
    # 关键服务名匹配情况
    names = " ".join(str(s.get("name", "")) for s in items if isinstance(s, dict)).lower()
    for k in ("docker", "smbd", "samba", "nfs", "nfsd", "ssh", "sshd", "avahi"):
        print(f"    contains {k}: {k in names}")


async def main() -> int:
    env = load_env()
    base = f"https://{env.get('UGOS_HOST', '192.0.2.10')}:{env.get('UGOS_PORT', '9443')}"
    c = UgosClient(base, env.get("UGOS_USER", ""), env.get("UGOS_PASSWORD", ""))
    try:
        await c.login()
        for name in (sys.argv[1:] or ["users"]):
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
