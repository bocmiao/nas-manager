#!/usr/bin/env python3
"""回收站 + 缩略图探测（只读）。

背景：App 现在删文件是进回收站的，但既看不到也恢复不了——功能不闭环。
这里确认三件事能不能做：回收站配置、回收站列表、文件缩略图。

用法: python scripts/probe_recycle.py
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

OUT = os.path.join(ROOT, ".probe")


def dump(name, payload):
    with open(os.path.join(OUT, f"rc_{name}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def brief(value, limit=400):
    text = json.dumps(value, ensure_ascii=False)
    return text[:limit] + ("…" if len(text) > limit else "")


async def main() -> int:
    env = load_env()
    client = UgosClient(
        base=f"https://{env['UGOS_HOST']}:{int(env.get('UGOS_PORT', 9999))}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    await client.login()

    print("[1] 回收站配置")
    for label, path in [
        ("getRecycleConf", "v1/filemgr/getRecycleConf"),
        ("getRecycleRootDirs", "v1/filemgr/getRecycleRootDirs"),
        ("getVolumes", "v1/filemgr/getVolumes"),
    ]:
        try:
            data = await client.get(path)
            print(f"  [ok] {label:22s} {brief(data)}")
            dump(label, data)
        except UgosError as exc:
            print(f"  [x] {label:22s} code={exc.code} {exc.msg}")

    print("\n[2] 回收站根目录列表（getDirFileListV2 + is_access_recycle）")
    roots = None
    try:
        roots = json.load(open(os.path.join(OUT, "rc_getRecycleRootDirs.json"), encoding="utf-8"))
    except FileNotFoundError:
        pass

    base_body = {
        "data_type": 2,
        "left_no_page_show": False,
        "left_count": 5000,
        "version": 1,
        "operate": 0,
        "intranet_share_id": 0,
        "is_access_recycle": True,
        "page": 1,
        "page_size": 50,
    }
    for body in (base_body, {**base_body, "path": "/"}, {**base_body, "root_type": 2}):
        tag = "path=" + str(body.get("path")) if "path" in body else f"root_type={body.get('root_type')}" if "root_type" in body else "无路径"
        try:
            data = await client.post("v2/filemgr/getDirFileListV2", body)
            files = (data or {}).get("files") or []
            print(f"  [ok] {tag:16s} 条目={len(files)} total={(data or {}).get('total')}")
            if files:
                dump("dirlist", data)
                print(f"       首条字段: {sorted(files[0].keys())[:14]}")
            break
        except UgosError as exc:
            print(f"  [x] {tag:16s} code={exc.code} {exc.msg}")

    print("\n[3] 缩略图（先找一个图片文件）")
    target = None
    for root_type in (2, 3):
        body = {**base_body, "root_type": root_type, "is_access_recycle": False}
        body.pop("is_access_recycle")
        try:
            data = await client.post("v2/filemgr/getDirFileListV2", body)
        except UgosError:
            continue
        for f in (data or {}).get("files") or []:
            name = str(f.get("name") or "")
            if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".heic")):
                target = f
                break
        if target:
            break

    if not target:
        print("  跳过：根目录下没找到图片文件")
    else:
        path = target.get("path") or ""
        print(f"  目标: {path}")
        params = {
            "path": path,
            "type": target.get("type") or 2,
            "size_type": 0,
            "mtime": target.get("mtime") or 0,
            "ctime": target.get("ctime") or 0,
            "file_size": target.get("size") or 0,
            "intranet_share_id": 0,
            "width": 64,
            "height": 64,
        }
        try:
            async with client.open_stream("v1/filemgr/thumbnail", params) as resp:
                print(f"  HTTP {resp.status_code} type={resp.headers.get('content-type')} len={resp.headers.get('content-length')}")
                chunk = b""
                async for piece in resp.aiter_bytes(4096):
                    chunk += piece
                    if len(chunk) >= 256:
                        break
                print(f"  前 16 字节: {chunk[:16].hex()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [x] 缩略图失败: {type(exc).__name__}: {exc}")

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
