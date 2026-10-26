#!/usr/bin/env python3
"""删除语义探测：`forever=false` 到底进不进回收站（真机实测）。

为什么必须验：`getRecycleConf` 返回 `dir_recycle: false`，而 UGOS 前端在
「目录未开回收站」时会强制传 `forever: true` 直接永久删除。如果 NAS 对
`forever: false` 也照删不误，那 App 上「删除（默认进回收站）」的提示就是
一句假话——用户以为能找回，实际永久没了。这是数据安全问题，不能靠猜。

只碰自己创建的测试文件，测完清理。

用法: python scripts/probe_delete_semantics.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nas_login_test import load_env  # noqa: E402

from app.ugos.client import UgosClient, UgosError  # noqa: E402

OUT = os.path.join(ROOT, ".probe")
TEST_DIR_NAME = f"_nasmgr_deltest_{int(time.time())}"


def brief(value, limit=300):
    text = json.dumps(value, ensure_ascii=False)
    return text[:limit] + ("…" if len(text) > limit else "")


async def list_dir(client, path=None, root_type=None, recycle=False):
    body = {
        "limit": 100,
        "page": 1,
        "dir_recycle": False,
        "data_type": 2,
        "left_no_page_show": False,
        "left_count": 5000,
        "sort_type": 1,
        "reverse": False,
        "version": 1,
        "operate": 0,
        "intranet_share_id": 0,
        "permission": 0,
        "is_shield_recycle": False,
    }
    if recycle:
        body["is_access_recycle"] = True
    if root_type is not None:
        body["root_type"] = root_type
        # 共享文件夹根必须带 permission=4，否则返回空列表（不报错，最坑）
        if root_type == 3:
            body["permission"] = 4
    if path is not None:
        body["path"] = path
    return await client.post("v2/filemgr/getDirFileListV2", body) or {}


def names(data):
    """注意：文件在 `right_files.files` 里，不是顶层 `files`。"""
    right = data.get("right_files") or {}
    return [f.get("name") for f in (right.get("files") or []) if isinstance(f, dict)]


async def main() -> int:
    env = load_env()
    client = UgosClient(
        base=f"https://{env['UGOS_HOST']}:{int(env.get('UGOS_PORT', 9999))}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    await client.login()

    print("[0] 回收站配置")
    conf = await client.get("v1/filemgr/getRecycleConf") or {}
    print(f"    {brief(conf, 200)}")
    print(f"    → dir_recycle={conf.get('dir_recycle')}（目录回收站）")

    print("\n[1] 找一个可写的共享目录")
    base = None
    try:
        data = await list_dir(client, root_type=3)
        for f in (data.get("right_files") or {}).get("files") or []:
            if f.get("file_type") == 1 and f.get("path"):
                base = f["path"]
                break
    except UgosError as exc:
        print(f"    [x] {exc.code} {exc.msg}")
    if not base:
        print("    找不到可写共享目录，放弃")
        await client.close()
        return 1
    print(f"    基准目录: {base}")

    target = f"{base}/{TEST_DIR_NAME}"
    print(f"\n[2] 建测试目录 + 文件")
    try:
        await client.post("v2/filemgr/createFolder", {"path": target})
        print(f"    目录已建")
    except UgosError as exc:
        print(f"    [x] 建目录失败 {exc.code} {exc.msg}")
        await client.close()
        return 1

    payload = b"nasmgr delete semantics probe\n"
    token = await client.password_token()
    n = len(payload)
    for offset in range(0, n, 1024):
        piece = payload[offset:offset + 1024]
        param = {
            "uuid": __import__("hashlib").md5(b"probe.txt").hexdigest(),
            "file_name": "probe.txt",
            "action_type": 3,
            "size": n,
            "current_size": len(piece),
            "resume": True,
            "dir": target.replace("/", "%2F"),
            "change_time": int(time.time()),
            "is_live_photo": False,
            "first_request": False,
            "begin_size": offset,
        }
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="probe.txt"',
            "ug-param": json.dumps(param, ensure_ascii=True, separators=(",", ":")),
        }
        await client.post_raw("v1/filemgr/fileUploadV2", piece, headers=headers)
    after_upload = names(await list_dir(client, path=target))
    print(f"    目录内文件: {after_upload}")
    if "probe.txt" not in after_upload:
        print("    [x] 上传没成功，跳过删除测试")
        await client.close()
        return 1

    print("\n[3] 用 forever=false 删除（App 当前的行为）")
    file_path = f"{target}/probe.txt"
    result = await client.post("v2/filemgr/delPaths", {"paths": [file_path], "forever": False})
    print(f"    返回: {brief(result, 260)}")
    uuid = None
    if isinstance(result, dict):
        uuid = result.get("result") or result.get("uuid")

    if uuid:
        print(f"    删除是异步任务，uuid={uuid}")
        for _ in range(10):
            await asyncio.sleep(1)
            try:
                info = await client.get("v2/filemgr/getDelTaskInfo", {"uuid": uuid})
                txt = brief(info, 200)
                print(f"    进度: {txt}")
                body = (info or {}).get("result") or {}
                if isinstance(body, dict) and body.get("status") in (2, 3, "done", "finish"):
                    break
            except UgosError as exc:
                print(f"    [x] {exc.code} {exc.msg}")
                break
    else:
        await asyncio.sleep(2)

    print("\n[4] 文件还在吗")
    still = names(await list_dir(client, path=target))
    print(f"    目录内: {still}")
    if "probe.txt" in still:
        print("    → 删除未生效")
    else:
        print("    → 文件已从目录消失")

    print("\n[5] 回收站里能看到吗")
    for attempt in (
        dict(root_type=3, recycle=True),
        dict(root_type=2, recycle=True),
    ):
        try:
            data = await list_dir(client, **attempt)
            print(f"    {attempt} → 条目={len((data.get('right_files') or {}).get('files') or [])} {names(data)[:5]}")
            with open(os.path.join(OUT, "del_recycle_list.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            break
        except UgosError as exc:
            print(f"    {attempt} → code={exc.code} {exc.msg}")

    print("\n[6] 清理测试目录")
    try:
        await client.post(
            "v2/filemgr/delPaths", {"paths": [target], "forever": True}
        )
        print("    已永久删除测试目录")
    except UgosError as exc:
        print(f"    [x] 清理失败 {exc.code} {exc.msg} —— 请手动删除 {TEST_DIR_NAME}")

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
