#!/usr/bin/env python3
"""缩略图 + 文件异步搜索探测（只读）。

这两个是文件管理最明显的体验缺口：
- 现在所有图片都只显示一个通用图标，看不出内容
- 文件一多只能靠翻页找，没有搜索

用法: python scripts/probe_thumb_search.py
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

BASE = {
    "limit": 100, "page": 1, "dir_recycle": False, "data_type": 2,
    "left_no_page_show": False, "left_count": 5000, "sort_type": 1,
    "reverse": False, "version": 1, "operate": 0, "intranet_share_id": 0,
    "permission": 0, "is_shield_recycle": False,
}


async def list_path(client, path):
    body = dict(BASE)
    body["path"] = path
    return await client.post("v2/filemgr/getDirFileListV2", body) or {}


async def main() -> int:
    env = load_env()
    client = UgosClient(
        base=f"https://{env['UGOS_HOST']}:{int(env.get('UGOS_PORT', 9999))}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    token = await client.login()
    print(f"登录成功，token 长度={len(token)}")

    print("\n[1] 找一个图片文件")
    image = None
    candidates = []
    for rt, perm in ((3, 4), (2, None), (4, None)):
        body = dict(BASE)
        body["root_type"] = rt
        if perm:
            body["permission"] = perm
        try:
            data = await client.post("v2/filemgr/getDirFileListV2", body) or {}
        except UgosError:
            continue
        for f in (data.get("right_files") or {}).get("files") or []:
            if f.get("file_type") == 1 and f.get("path"):
                candidates.append(f["path"])
    # 再往下一层找图片
    for folder in candidates[:6]:
        try:
            data = await list_path(client, folder)
        except UgosError:
            continue
        for f in (data.get("right_files") or {}).get("files") or []:
            ext = str(f.get("ext") or "").lower().lstrip(".")
            if ext in ("jpg", "jpeg", "png", "webp", "heic", "bmp"):
                image = f
                break
        if image:
            break

    if not image:
        print("    没找到图片，跳过缩略图测试")
    else:
        print(f"    找到: {image.get('ext')}  size={image.get('size')}")
        # 缩略图靠 token 鉴权（前端是 <img src> 带 cookie，我们用 query token）
        params = {
            "path": image.get("path"),
            "type": image.get("type") or 2,
            "size_type": 0,
            "mtime": image.get("mtime") or 0,
            "ctime": image.get("ctime") or 0,
            "file_size": image.get("size") or 0,
            "intranet_share_id": image.get("intranet_share_id") or 0,
            "width": 96,
            "height": 96,
            "token": token,
        }
        try:
            async with client.open_stream("v1/filemgr/thumbnail", params) as resp:
                head = b""
                async for piece in resp.aiter_bytes(2048):
                    head += piece
                    if len(head) >= 64:
                        break
                ctype = resp.headers.get("content-type")
                print(f"    HTTP {resp.status_code}  type={ctype}  前16字节={head[:16].hex()}")
                is_jpeg = head[:2] == b"\xff\xd8"
                is_png = head[:8] == b"\x89PNG\r\n\x1a\n"
                is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
                print(f"    → 是图片吗: jpeg={is_jpeg} png={is_png} webp={is_webp}")
        except Exception as exc:  # noqa: BLE001
            print(f"    [x] {type(exc).__name__}: {exc}")

    print("\n[2] 异步搜索（createsearchtask）")
    search_body = {
        "keyword": "probe",
        "path": "/volume1/docker",
        "page": 1,
        "limit": 20,
    }
    try:
        res = await client.post("v2/filemgr/createsearchtask", search_body)
        print(f"    创建: {json.dumps(res, ensure_ascii=False)[:260]}")
        task = (res or {}).get("result")
        if task:
            for i in range(6):
                await asyncio.sleep(1)
                try:
                    out = await client.post(
                        "v2/filemgr/getsearchtaskresult",
                        {"task_id": task, "page": 1, "limit": 20},
                    )
                    txt = json.dumps(out, ensure_ascii=False)
                    print(f"    [{i+1}s] {txt[:260]}")
                    if '"finish"' in txt or '"done"' in txt or "files" in txt:
                        break
                except UgosError as exc:
                    print(f"    [x] 结果查询 {exc.code} {exc.msg}")
                    break
    except UgosError as exc:
        print(f"    [x] 创建失败 {exc.code} {exc.msg}")

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
