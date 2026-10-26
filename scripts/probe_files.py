#!/usr/bin/env python3
"""文件管理接口探测（根目录 / 目录列表 / 下载令牌 / 建文件夹）。

用法:
    python scripts/probe_files.py

结果写到 .probe/files_*.json（已 gitignore，可含真实设备数据）。
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
    path = os.path.join(PROBE_DIR, f"files_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"  -> {path}")


async def call(c: UgosClient, method: str, path: str, **kw) -> dict:
    rec = {"method": method.upper(), "path": path,
           "args": kw.get("params") or kw.get("body")}
    try:
        if method == "get":
            data = await c.get(path, kw.get("params"))
        else:
            data = await c.post(path, kw.get("body"), params=kw.get("params"))
        rec["ok"] = True
        rec["data"] = data
    except UgosError as exc:
        rec["ok"] = False
        rec["code"] = exc.code
        rec["msg"] = exc.msg
    except Exception as exc:  # noqa: BLE001
        rec["ok"] = False
        rec["error"] = f"{type(exc).__name__}: {exc}"
    tail = "" if rec.get("ok") else f"code={rec.get('code')} {rec.get('msg') or rec.get('error')}"
    print(f"  {'OK ' if rec.get('ok') else 'ERR'} {rec['method']:6s} {path} {tail}")
    return rec


def brief(data, depth=0):
    """只保留结构，避免把大列表全打出来。"""
    if isinstance(data, dict):
        return {k: brief(v, depth + 1) for k, v in list(data.items())[:30]}
    if isinstance(data, list):
        return [brief(data[0], depth + 1)] if data else []
    return data


async def main() -> None:
    env = load_env()
    base = f"https://{env['UGOS_HOST']}:{env['UGOS_PORT']}"
    c = UgosClient(base=base, username=env["UGOS_USER"], password=env["UGOS_PASSWORD"])
    await c.login()
    print("登录成功")

    # 1) 根目录列表
    roots = await call(c, "get", "v2/filemgr/getRootDirs", params={"client": "pc"})
    dump("roots", roots)
    if roots.get("ok"):
        rl = (roots["data"] or {}).get("data", {}).get("root_list")
        if rl:
            print(f"  根条目 {len(rl)} 个: {[ (x.get('name'), x.get('category'), x.get('path'), x.get('id')) for x in rl][:12]}")

    # 2) 卷列表（拿真实挂载路径）
    vols = await call(c, "get", "v1/filemgr/getVolumes")
    dump("volumes", vols)
    vol_paths = []
    if vols.get("ok"):
        for v in (vols["data"] or {}).get("data", {}).get("result", []) or []:
            vol_paths.append(v.get("path"))
            print(f"    vol: path={v.get('path')} name={v.get('name')} fs={v.get('fs_type')} "
                  f"free={v.get('free')} all={v.get('all')}")

    # 3) 共享文件夹（拿准确的 path）
    shares = await call(c, "get", "v1/filemgr/getSharedFolderList")
    dump("shares", shares)

    # 4) 目录列表 —— 根级用 root_type（数字），子路径用 path（字符串）
    roots_raw = ((roots.get("data") or {}).get("data") or {}).get("root_list") or [2, 3, 4, 6, 9]
    list_results = []
    for rt in roots_raw:
        body = {
            "limit": 100, "page": 1, "is_shield_recycle": False,
            "data_type": 2, "left_no_page_show": False, "left_count": 5000,
            "root_type": rt, "sort_type": 1, "reverse": False,
            "intranet_share_id": 0, "permission": 4 if rt == 3 else 0,
            "version": 1, "operate": 0,
        }
        rec = await call(c, "post", "v2/filemgr/getDirFileListV2", body=body)
        rec["root_type"] = rt
        list_results.append(rec)
        if rec.get("ok"):
            d = rec["data"].get("data") or {}
            rf = d.get("right_files") or {}
            print(f"     keys={list(d.keys())} total={rf.get('total')} n={len(rf.get('list') or [])}")
            for item in (rf.get("list") or [])[:3]:
                print(f"       - name={item.get('name')} is_folder={item.get('is_folder')} "
                      f"size={item.get('size')} path={item.get('path')}")
            if rf.get("list"):
                print(f"     首项全字段: {sorted(rf['list'][0].keys())}")
    dump("list", list_results)

    # 5) v1 列表（老接口，可能仍可用）
    if list_results and list_results[0].get("ok"):
        p = cands[0]
        old = await call(c, "post", "v1/filemgr/getDirFileList",
                         body={"path": p, "page": 1, "limit": 100})
        dump("list_v1", old)

    # 6) 下载令牌链路：先 addPathsTask，再 getDownloadToken
    if list_results and list_results[0].get("ok"):
        d = list_results[0]["data"].get("data") or {}
        files = (d.get("right_files") or {}).get("list") or []
        target = next((f for f in files if not f.get("is_folder")), None)
        if target:
            paths = [{"path": target.get("path"), "intranet_share_id": target.get("intranet_share_id") or 0}]
            task = await call(c, "post", "v2/filemgr/addPathsTask", body={"paths": paths})
            dump("addtask", task)
            uuid = ((task.get("data") or {}).get("data") or {}).get("result")
            print(f"     task uuid = {uuid}")
            if uuid:
                tok = await call(c, "get", "v2/filemgr/getDownloadToken",
                                 params={"task_uuid": str(uuid), "coding": "true"})
                dump("dltoken", tok)
                dl = ((tok.get("data") or {}).get("data") or {}).get("dl_token")
                print(f"     dl_token = {(dl or '')[:24]}... len={len(dl or '')}")
        else:
            print("  目录里没有可下载的文件，跳过下载链路")

    # 7) 配额 / 空间
    await call(c, "post", "v1/filemgr/getPathFreeSize", body={"path": cands[0] if cands else "/"})

    await c.close()
    print("\n完成")


if __name__ == "__main__":
    asyncio.run(main())
