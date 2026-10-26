#!/usr/bin/env python3
"""复制 / 移动任务的完成判定。

上一轮探针确认了：
- `src` 必须是**字符串数组**（对象数组回 9999）
- `type` `true`=移动 / `false`=复制
- 返回 `{path: "", task_id: "<uuid>"}` —— 是**异步任务**，不是同步完成

所以真正要回答的是：**任务什么时候算完**。这里试两条判定路：
  A. 轮询目标目录，看文件什么时候出现（最笨也最可靠）
  B. 拿 `task_id` 问 `getDelTaskInfo`，看它认不认这类任务

顺带测 `action_type=3`（另存改名）在目标已存在同名时会不会真的改名。
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
TMP_DIR = "_nasmgr_cpmv2"
FNAME = "hello.txt"


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

    sandbox = f"{parent}/{TMP_DIR}"
    src_dir = f"{sandbox}/src"
    dest = f"{sandbox}/dest"
    for p in (sandbox, src_dir, dest):
        try:
            await client.post("v2/filemgr/createFolder", {"path": p})
        except UgosError as exc:
            print(f"建目录 {p} → {exc.code} {exc.msg}")
    print(f"[0] 试验田 {sandbox}")

    payload = b"copy-move probe 2\n"
    param = {
        "uuid": hashlib.md5(FNAME.encode()).hexdigest(),
        "file_name": FNAME,
        "action_type": 3,
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

    # ---- 复制 ----
    print("\n[2] 复制（type=false）")
    r = await client.post("v2/filemgr/cpOrMvPath", {
        "dst": dest, "src": [f"{src_dir}/{FNAME}"],
        "action_type": 3, "type": False, "intranet_share_id": 0,
    }) or {}
    task_id = r.get("task_id") or ""
    print(f"    task_id={task_id or '(空)'}")
    ok, elapsed, names = await wait_for(client, dest, lambda n: FNAME in n)
    print(f"    目标目录在 {elapsed:.1f}s 后出现文件: {ok} → {names}")

    print("\n[3] 用 getDelTaskInfo 查这个 task_id 认不认")
    if task_id:
        for params in (
            {"task_id": task_id},
            {"task_uuid": task_id},
            {"uuid": task_id},
        ):
            try:
                info = await client.get("v2/filemgr/getDelTaskInfo", params) or {}
                print(f"    {params} → {info}")
            except UgosError as exc:
                print(f"    {params} → {exc.code} {exc.msg}")

    # ---- 再复制一次，目标已有同名，看 action_type=3 会不会改名 ----
    print("\n[4] 目标已存在同名，再复制一次（action_type=3 另存）")
    await client.post("v2/filemgr/cpOrMvPath", {
        "dst": dest, "src": [f"{src_dir}/{FNAME}"],
        "action_type": 3, "type": False, "intranet_share_id": 0,
    })
    ok, elapsed, names = await wait_for(client, dest, lambda n: len(n) >= 2)
    print(f"    {elapsed:.1f}s 后目标目录: {names}")

    # ---- 移动 ----
    print("\n[5] 移动（type=true）")
    await client.post("v2/filemgr/cpOrMvPath", {
        "dst": dest, "src": [f"{src_dir}/{FNAME}"],
        "action_type": 3, "type": True, "intranet_share_id": 0,
    })
    ok, elapsed, names = await wait_for(client, src_dir, lambda n: FNAME not in n)
    print(f"    src 在 {elapsed:.1f}s 后不再有该文件: {ok} → {names}")
    print(f"    dest 现在: {await listing(client, dest)}")

    print("\n[6] 清理")
    try:
        await client.post("v2/filemgr/delPaths", {"paths": [sandbox], "forever": True})
        print("    已删除试验田")
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg} —— 请手动删除 {sandbox}")

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
