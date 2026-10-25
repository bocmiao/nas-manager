#!/usr/bin/env python3
"""批量探测脚本（第二批功能开发用）。

用法:
    python scripts/probe_batch2.py modules          # 列已装应用入口
    python scripts/probe_batch2.py images           # 探测镜像接口
    python scripts/probe_batch2.py snapshot         # 探测快照接口
    python scripts/probe_batch2.py firmware ups container

结果写到 .probe/batch2_<name>.json（已 gitignore，可含真实设备数据）。
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
    path = os.path.join(PROBE_DIR, f"batch2_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n-> {path}")


async def try_call(c: UgosClient, method: str, path: str, **kw) -> dict:
    """单次调用，异常也记录成结构化结果。"""
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


def brief(data, depth: int = 0, max_list: int = 2):
    """压缩大结构便于看字段名。"""
    if isinstance(data, dict):
        return {k: brief(v, depth + 1, max_list) for k, v in list(data.items())[:40]}
    if isinstance(data, list):
        return [brief(x, depth + 1, max_list) for x in data[:max_list]]
    return data


# ---------------------------------------------------------------- modules

async def probe_modules(c: UgosClient) -> None:
    rec = await try_call(c, "get", "v1/desktop/module/list")
    dump("modules", rec)
    data = rec.get("data") or {}
    items = data.get("result") if isinstance(data, dict) else data
    if isinstance(items, list):
        print("\n应用入口:")
        for m in items:
            if isinstance(m, dict):
                print(f"  {str(m.get('module_id')):>4}  {str(m.get('name')):<14} "
                      f"entry={m.get('entry')} ver={m.get('version')}")


# ---------------------------------------------------------------- images

async def probe_images(c: UgosClient) -> None:
    out = []
    out.append(await try_call(c, "get", "v1/docker/image/ObtainAllImages"))
    out.append(await try_call(c, "post", "v1/docker/image/ObtainAllImages", body={}))
    out.append(await try_call(c, "post", "v1/docker/image/ObtainAllImages",
                              body={"PageNum": 1, "PageSize": 100}))
    out.append(await try_call(c, "get", "v1/docker/image/ShowLocalImageV2"))
    out.append(await try_call(c, "post", "v1/docker/image/ShowLocalImageV2", body={}))
    out.append(await try_call(c, "post", "v1/docker/image/ShowLocalImageV2",
                              body={"PageNum": 1, "PageSize": 100}))
    out.append(await try_call(c, "post", "v1/docker/container/QueryAllContaners", body={}))
    out.append(await try_call(c, "get", "v1/docker/image/GetCommonList"))
    dump("images", [dict(r, data=brief(r.get("data"))) for r in out])


# ---------------------------------------------------------------- snapshot

SNAPSHOT_PATHS = [
    "v1/storage/snapshot/list",
    "v2/storage/snapshot/list",
    "v1/storage/snapshot/getList",
    "v1/snapshot/list",
    "v1/snapshot/getList",
    "v1/storage/snapshot",
]


async def probe_snapshot(c: UgosClient) -> None:
    out = []
    for p in SNAPSHOT_PATHS:
        out.append(await try_call(c, "get", p, params={"page": 1, "size": 20}))
    out.append(await try_call(c, "post", "v1/storage/snapshot/list",
                              body={"PageNum": 1, "PageSize": 20}))
    dump("snapshot", [dict(r, data=brief(r.get("data"))) for r in out])


# ---------------------------------------------------------------- firmware / ups

async def probe_firmware(c: UgosClient) -> None:
    out = [
        await try_call(c, "get", "v1/firmware/status"),
        await try_call(c, "get", "v1/firmware/version/is_new"),
    ]
    dump("firmware", out)


async def probe_ups(c: UgosClient) -> None:
    out = [await try_call(c, "get", "v1/sysinfo/machine/common")]
    rec = out[0]
    hw = ((rec.get("data") or {}).get("hardware") or {})
    print("\nhardware.ups =", json.dumps(hw.get("ups"), ensure_ascii=False))
    print("hardware keys =", list(hw.keys()))
    dump("ups", out)


# ---------------------------------------------------------------- container detail

async def probe_container(c: UgosClient) -> None:
    lst = await c.post("v1/docker/container/ContainerListV2", {"PageNum": 1, "PageSize": 5})
    items = (lst or {}).get("result") or []
    out = []
    if not items:
        print("没有容器可探测")
        return
    cid = items[0].get("containerId")
    print(f"用容器 {items[0].get('name')} ({cid[:12]}) 探测")
    out.append(await try_call(c, "get", "v1/docker/container/GetContainerById",
                              params={"containerId": cid}))
    out.append(await try_call(c, "post", "v1/docker/container/GetContainerById",
                              body={"containerId": cid}))
    out.append(await try_call(c, "get", "v1/docker/container/ShowContainerDetailListV2",
                              params={"containerId": cid, "PageNum": 1, "PageSize": 10}))
    out.append(await try_call(c, "post", "v1/docker/container/ShowContainerDetailListV2",
                              body={"PageNum": 1, "PageSize": 10}))
    dump("container", [dict(r, data=brief(r.get("data"))) for r in out])


async def main() -> int:
    env = load_env()
    base = f"https://{env.get('UGOS_HOST', '192.0.2.10')}:{env.get('UGOS_PORT', '9443')}"
    c = UgosClient(base, env.get("UGOS_USER", ""), env.get("UGOS_PASSWORD", ""))
    try:
        await c.login()
        for name in (sys.argv[1:] or ["modules"]):
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
