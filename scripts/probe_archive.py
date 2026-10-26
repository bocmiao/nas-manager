#!/usr/bin/env python3
"""压缩 / 解压（`compressedFile` / `unCompressedFile`）的真机契约验证。

前端反出来两个载荷形状：

压缩 `POST v1/filemgr/compressedFile`
  {name: <包名不含后缀>, ext: "zip", conflict_options: 0, out: <输出目录>, paths: [<路径>]}

解压 `POST v1/filemgr/unCompressedFile`
  {out: <目标目录>, path: <压缩包路径>, action_type: <冲突策略>, password?: <密码>}
  前置可选：`POST v1/filemgr/isEncryptZip {path}` → `data.result` 为真表示要密码

支持的格式（前端静态表）：zip / rar / 7z / tar / gz / tgz / iso

这里跑一遍完整闭环：建沙盒 → 塞文件 → 压成 zip → 解到另一个目录 → 校验 → 全删。
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
TMP = "_nasmgr_zip"
FNAME = "note.txt"


async def listing(client: UgosClient, path: str) -> list[str]:
    data = await client.post(
        "v2/filemgr/getDirFileListV2", dict(LIST_BASE, path=path)
    ) or {}
    files = (data.get("right_files") or {}).get("files") or []
    return sorted(f.get("name") or "" for f in files)


async def wait_for(client, path: str, predicate, timeout: float = 25.0) -> tuple[bool, float, list[str]]:
    start = time.time()
    last: list[str] = []
    while time.time() - start < timeout:
        last = await listing(client, path)
        if predicate(last):
            return True, time.time() - start, last
        await asyncio.sleep(0.5)
    return False, time.time() - start, last


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
    parent = None
    for f in (data.get("right_files") or {}).get("files") or []:
        if f.get("file_type") == 1 and f.get("path"):
            parent = f["path"]
            break
    if not parent:
        print("找不到可写共享目录")
        await client.close()
        return 1

    sandbox = f"{parent}/{TMP}"
    src_dir = f"{sandbox}/src"
    out_dir = f"{sandbox}/out"
    for p in (sandbox, src_dir, out_dir):
        try:
            await client.post("v2/filemgr/createFolder", {"path": p})
        except UgosError as exc:
            print(f"建目录 {p} → {exc.code} {exc.msg}")
    print(f"[0] 沙盒 {sandbox}")

    payload = b"archive probe\nline two\n"
    param = {
        "uuid": hashlib.md5(FNAME.encode()).hexdigest(),
        "file_name": FNAME, "action_type": 3,
        "size": len(payload), "current_size": len(payload), "resume": True,
        "dir": src_dir.replace("/", "%2F"),
        "change_time": int(time.time()),
        "is_live_photo": False, "first_request": False, "begin_size": 0,
    }
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="{FNAME}"',
        "ug-param": json.dumps(param, ensure_ascii=True, separators=(",", ":")),
    }
    await client.post_raw("v1/filemgr/fileUploadV2", payload, headers=headers)
    print(f"[1] 已上传 {src_dir}/{FNAME}")

    print("\n[2] 压缩成 zip")
    zip_name = "probe"
    try:
        r = await client.post("v1/filemgr/compressedFile", {
            "name": zip_name, "ext": "zip", "conflict_options": 0,
            "out": src_dir, "paths": [f"{src_dir}/{FNAME}"],
        })
        print(f"    返回: {r}")
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg}")
    ok, elapsed, names = await wait_for(
        client, src_dir, lambda n: any(x.endswith(".zip") for x in n)
    )
    zip_file = next((x for x in names if x.endswith(".zip")), "")
    print(f"    {elapsed:.1f}s 后出现压缩包: {ok} → {names}")
    if not zip_file:
        print("    拿不到压缩包，后面的解压没法测")
        await client.post("v2/filemgr/delPaths", {"paths": [sandbox], "forever": True})
        await client.close()
        return 1
    zip_path = f"{src_dir}/{zip_file}"

    print("\n[3] isEncryptZip")
    try:
        r = await client.post("v1/filemgr/isEncryptZip", {"path": zip_path})
        print(f"    → {r}")
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg}")

    print("\n[4] getArchiveFileInfo")
    for payload0 in ({"path": zip_path}, {"paths": [zip_path]}):
        try:
            r = await client.get("v2/filemgr/getArchiveFileInfo", payload0)
            print(f"    {payload0} → {str(r)[:200]}")
            break
        except UgosError as exc:
            print(f"    {payload0} → {exc.code} {exc.msg}")

    print("\n[5] 解压到 out 目录")
    try:
        r = await client.post("v1/filemgr/unCompressedFile", {
            "out": out_dir, "path": zip_path, "action_type": 0,
        })
        print(f"    返回: {r}")
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg}")
    ok, elapsed, names = await wait_for(
        client, out_dir, lambda n: FNAME in n, timeout=30
    )
    print(f"    {elapsed:.1f}s 后 out 目录: {ok} → {names}")

    print("\n[6] 清理")
    try:
        await client.post("v2/filemgr/delPaths", {"paths": [sandbox], "forever": True})
        print("    已删除沙盒")
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg} —— 请手动删除 {sandbox}")

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
