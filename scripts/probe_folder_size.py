#!/usr/bin/env python3
"""`getSizeAndCount` 的载荷形状验证 —— 文件夹到底占多大。

列表里所有目录的大小都显示 `--`，因为 `getDirFileListV2` 不返回目录体积。
UGOS 网页端是用 `v2/filemgr/getSizeAndCount` 现算的，前端把 params 原样透传，
看不出具体字段，这里试几种最常见的形状。

顺带看 `delSizeAndCount`（取消计算）要不要 `calculate_id`。
只读操作，不改任何文件。
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nas_login_test import load_env  # noqa: E402

from app.ugos.client import UgosClient, UgosError  # noqa: E402

LIST_BASE = {
    "limit": 50, "page": 1, "dir_recycle": False, "data_type": 2,
    "left_no_page_show": False, "left_count": 5000, "sort_type": 1,
    "reverse": False, "version": 1, "operate": 0, "intranet_share_id": 0,
    "permission": 0, "is_shield_recycle": False,
}


async def main() -> int:
    env = load_env()
    client = UgosClient(
        base=f"https://{env['UGOS_HOST']}:{int(env.get('UGOS_PORT', 9999))}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    await client.login()

    data = await client.post(
        "v2/filemgr/getDirFileListV2", dict(LIST_BASE, root_type=3, permission=4)
    ) or {}
    target = None
    for f in (data.get("right_files") or {}).get("files") or []:
        if f.get("file_type") == 1 and f.get("path"):
            target = f
            break
    if not target:
        print("找不到可读目录")
        await client.close()
        return 1
    print(f"[目标目录] {target.get('name')} → {target.get('path')}")

    candidates = [
        {"path": target["path"]},
        {"paths": [target["path"]]},
        {"path": target["path"], "intranet_share_id": 0},
        {"paths": [target["path"]], "intranet_share_id": 0},
        {"path": target["path"], "calculate_type": 0},
        {"path": target["path"], "size_type": 0},
    ]
    for params in candidates:
        try:
            r = await client.get("v2/filemgr/getSizeAndCount", params)
            print(f"  {params} → {str(r)[:220]}")
        except UgosError as exc:
            print(f"  {params} → {exc.code} {exc.msg}")

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
