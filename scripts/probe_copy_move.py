#!/usr/bin/env python3
"""复制 / 移动（`v2/filemgr/cpOrMvPath`）的真机契约验证。

App 目前只能新建 / 改名 / 删除 / 上传 / 下载，**没有复制和移动**，
这是文件管理器最基础的缺口。前端压缩过，看不出 `src` 到底要字符串数组
还是对象数组，所以这里直接试：

  A. `src` 传字符串数组
  B. `src` 传对象数组（像 `addPathsTask` 那样带 intranet_share_id）

顺带确认：
  - `type` 到底是不是 true=移动 / false=复制
  - `action_type` 0=跳过 1=覆盖 3=另存（同名自动改名）哪个在不冲突时也安全
  - 目标目录已存在同名时，action_type=3 会不会真的改名

全过程只在自己的临时目录里折腾，最后整个删掉。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nas_login_test import load_env  # noqa: E402

from app.ugos.client import UgosClient, UgosError  # noqa: E402

LIST_BASE = {
    "limit": 200, "page": 1, "dir_recycle": False, "data_type": 2,
    "left_no_page_show": False, "left_count": 5000, "sort_type": 1,
    "reverse": False, "version": 1, "operate": 0, "intranet_share_id": 0,
    "permission": 0, "is_shield_recycle": False,
}
TMP_DIR = "_nasmgr_mv_probe"


def names(data: dict) -> list[str]:
    files = (data.get("right_files") or {}).get("files") or []
    return sorted(f.get("name") or "" for f in files)


async def listing(client: UgosClient, path: str) -> list[str]:
    data = await client.post(
        "v2/filemgr/getDirFileListV2", dict(LIST_BASE, path=path)
    ) or {}
    return names(data)


async def main() -> int:
    env = load_env()
    client = UgosClient(
        base=f"https://{env['UGOS_HOST']}:{int(env.get('UGOS_PORT', 9999))}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    await client.login()

    # 找一个可写共享目录当试验田
    data = await client.post(
        "v2/filemgr/getDirFileListV2", dict(LIST_BASE, root_type=3, permission=4)
    ) or {}
    parent = None
    for f in (data.get("right_files") or {}).get("files") or []:
        if f.get("file_type") == 1 and f.get("path"):
            parent = f["path"]
            break
    if not parent:
        print("找不到可写共享目录")
        await client.close()
        return 1

    sandbox = f"{parent}/{TMP_DIR}"
    dest = f"{sandbox}/dest"
    src_dir = f"{sandbox}/src"

    print(f"[0] 试验田 {sandbox}")
    for p in (sandbox, dest, src_dir):
        try:
            await client.post("v2/filemgr/createFolder", {"path": p})
        except UgosError as exc:
            print(f"    建目录失败 {p}: {exc.code} {exc.msg}")

    # 塞一个文件进去
    payload = b"copy-move probe\n"
    fname = "hello.txt"
    param = {
        "uuid": hashlib.md5(fname.encode()).hexdigest(),
        "file_name": fname,
        "action_type": 3,
        "size": len(payload),
        "current_size": len(payload),
        "resume": True,
        "dir": src_dir.replace("/", "%2F"),
        "change_time": int(time.time()),
        "is_live_photo": False,
        "first_request": False,
        "begin_size": 0,
    }
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="{fname}"',
        "ug-param": json.dumps(param, ensure_ascii=True, separators=(",", ":")),
    }
    try:
        await client.post_raw("v1/filemgr/fileUploadV2", payload, headers=headers)
        print(f"[1] 已上传 {src_dir}/{fname}")
    except UgosError as exc:
        print(f"    [x] 上传失败 {exc.code} {exc.msg}")
        await client.close()
        return 1

    print(f"    src 目录内容: {await listing(client, src_dir)}")

    # A. 字符串数组 + type=false（复制）
    print("\n[2] A 方案：src 传字符串数组，type=false（复制）")
    try:
        r = await client.post(
            "v2/filemgr/cpOrMvPath",
            {
                "dst": dest,
                "src": [f"{src_dir}/{fname}"],
                "action_type": 3,
                "type": False,
                "intranet_share_id": 0,
            },
        )
        print(f"    返回: {r}")
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg}")
    print(f"    src:  {await listing(client, src_dir)}")
    print(f"    dest: {await listing(client, dest)}")

    # B. 对象数组
    print("\n[3] B 方案：src 传对象数组，type=false（复制）")
    try:
        r = await client.post(
            "v2/filemgr/cpOrMvPath",
            {
                "dst": dest,
                "src": [{"path": f"{src_dir}/{fname}", "intranet_share_id": 0}],
                "action_type": 3,
                "type": False,
                "intranet_share_id": 0,
            },
        )
        print(f"    返回: {r}")
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg}")
    print(f"    src:  {await listing(client, src_dir)}")
    print(f"    dest: {await listing(client, dest)}")

    # C. 移动：type=true
    print("\n[4] C 方案：type=true（移动）")
    try:
        r = await client.post(
            "v2/filemgr/cpOrMvPath",
            {
                "dst": dest,
                "src": [f"{src_dir}/{fname}"],
                "action_type": 3,
                "type": True,
                "intranet_share_id": 0,
            },
        )
        print(f"    返回: {r}")
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg}")
    print(f"    src:  {await listing(client, src_dir)}")
    print(f"    dest: {await listing(client, dest)}")

    print("\n[5] 清理试验田")
    try:
        await client.post("v2/filemgr/delPaths", {"paths": [sandbox], "forever": True})
        print("    已删除")
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg} —— 请手动删除 {sandbox}")

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
