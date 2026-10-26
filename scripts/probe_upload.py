#!/usr/bin/env python3
"""上传协议探测（预检 → 单块上传 → 校验 → 清理）。

上传走的是「分块可续传 + 参数塞 header」的协议，参数不在 body 里：
  Content-Type: application/octet-stream
  Content-Disposition: attachment; filename="<urlencoded 文件名>"
  ug-param: JSON
    {uuid, file_name, action_type, size, current_size, resume, dir,
     change_time, is_live_photo, first_request, begin_size}

本脚本只做**一次小文件探测**，成功后立即用已确认的 delPaths 删掉，
不在用户设备上留垃圾。结果写到 .probe/upload_*.json。

用法: python scripts/probe_upload.py [--keep]
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid as uuidlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nas_login_test import load_env  # noqa: E402

from app.ugos.client import UgosClient, UgosError  # noqa: E402

PROBE_DIR = os.path.join(ROOT, ".probe")
os.makedirs(PROBE_DIR, exist_ok=True)

# 目标目录：用探针一直在用的共享目录，写入权限已确认
DEST_DIR = "/volume1/docker/backup"
TEST_NAME = "_nasmgr_upload_probe.txt"
KEEP = "--keep" in sys.argv


def dump(name: str, payload) -> None:
    path = os.path.join(PROBE_DIR, f"upload_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    print(f"  -> {path}")


def list_body(path: str) -> dict:
    return {
        "limit": 200, "page": 1, "is_shield_recycle": False, "data_type": 2,
        "left_no_page_show": False, "left_count": 5000, "sort_type": 1,
        "reverse": False, "intranet_share_id": 0, "permission": 0,
        "version": 1, "operate": 0, "path": path,
    }


async def listdir(c: UgosClient, path: str) -> list[dict]:
    d = await c.post("v2/filemgr/getDirFileListV2", list_body(path))
    return (d.get("right_files") or {}).get("files") or []


def ug_param(uuid: str, name: str, size: int, current: int, begin: int,
             first: bool, action_type: int = 1) -> dict:
    return {
        "uuid": uuid,
        "file_name": name,              # 前端在此处 encodeURIComponent，httpx 会对 header 做 latin-1 编码，见下
        "action_type": action_type,
        "size": size,
        "current_size": current,
        "resume": True,
        "dir": DEST_DIR,
        # NAS 报错原文：cannot unmarshal number "" into Go struct field
        # UploadFileHeaderParamInfo.ChangeTime of type int64 —— 必须是 unix 秒
        "change_time": int(time.time()),
        "is_live_photo": False,
        "first_request": first,
        "begin_size": begin,
    }


def headers_for(param: dict, filename: str) -> dict:
    # filename 必须是 ASCII 安全形态：header 只能 latin-1，
    # 前端用 encodeURIComponent 处理，这里保持一致
    from urllib.parse import quote

    return {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="{quote(filename, safe="")}"',
        "ug-param": json.dumps(param, ensure_ascii=False, separators=(",", ":")),
    }


async def raw_post(c: UgosClient, path: str, body: bytes, headers: dict) -> dict:
    """直接打到 ugreen/ 前缀，token 走 query（与 UgosClient 的约定一致）。"""
    tok = await c._ensure_token()  # noqa: SLF001
    r = await c._http.post(  # noqa: SLF001
        c._url(path), content=body, params={"token": tok}, headers=headers,  # noqa: SLF001
    )
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = {"_raw": r.text[:300], "_status": r.status_code}
    return data


async def main() -> None:
    env = load_env()
    c = UgosClient(base=f"https://{env['UGOS_HOST']}:{env['UGOS_PORT']}",
                   username=env["UGOS_USER"], password=env["UGOS_PASSWORD"])
    await c.login()
    print("登录成功")

    content = b"nasmgr upload probe\n" * 40          # ~760 字节，单块足够
    size = len(content)
    name_md5 = hashlib.md5(TEST_NAME.encode()).hexdigest()

    # uuid 的三个候选：前端是 f(baseUuid, nameMd5)，具体组合方式未反出，逐个试
    candidates = [
        ("nameMd5", name_md5),
        ("uuid32", uuidlib.uuid4().hex),
        ("uuid-dash", str(uuidlib.uuid4())),
    ]

    before = await listdir(c, DEST_DIR)
    existed = any(f.get("name") == TEST_NAME for f in before)
    print(f"目标目录现有 {len(before)} 项，测试文件{'已存在' if existed else '不存在'}")

    ok_uuid = None
    for label, uid in candidates:
        print(f"\n--- 候选 uuid[{label}] = {uid[:18]}... ---")
        param = ug_param(uid, TEST_NAME, size, 0, 0, first=True)
        # 第一步：预检（空 body）
        try:
            pre = await raw_post(c, "v1/filemgr/fileUpload",
                                 b"", headers_for(param, TEST_NAME))
        except UgosError as exc:
            print(f"  预检 ERR {exc.code} {exc.msg}")
            continue
        print(f"  预检 -> {json.dumps(pre, ensure_ascii=False)[:260]}")
        dump(f"pre_{label}", pre)

        param2 = ug_param(uid, TEST_NAME, size, size, 0, first=False)
        try:
            up = await raw_post(c, "v1/filemgr/fileUploadV2",
                                content, headers_for(param2, TEST_NAME))
        except UgosError as exc:
            print(f"  上传 ERR {exc.code} {exc.msg}")
            continue
        print(f"  上传 -> {json.dumps(up, ensure_ascii=False)[:260]}")
        dump(f"chunk_{label}", up)

        code = up.get("code")
        after = await listdir(c, DEST_DIR)
        hit = next((f for f in after if f.get("name") == TEST_NAME), None)
        print(f"  目录校验：{'出现目标文件 size=' + str(hit.get('size')) if hit else '未出现'}")
        if hit or code == 200:
            ok_uuid = (label, uid, code, bool(hit))
            dump("result", {"uuid_kind": label, "code": code, "file_seen": bool(hit),
                            "file": hit})
            break

    print("\n=========== 结论 ===========")
    if ok_uuid:
        print(f"可用 uuid 形态：{ok_uuid[0]}，返回 code={ok_uuid[2]}，"
              f"文件{'已落盘' if ok_uuid[3] else '未在列表中出现'}")
    else:
        print("三个 uuid 候选都没成功，需要继续逆向 uuid 生成规则")

    # 清理
    after = await listdir(c, DEST_DIR)
    hit = next((f for f in after if f.get("name") == TEST_NAME), None)
    if hit and not KEEP:
        try:
            await c.post("v2/filemgr/delPaths", {"paths": [hit["path"]], "forever": True})
            check = await listdir(c, DEST_DIR)
            gone = not any(f.get("name") == TEST_NAME for f in check)
            print(f"清理：{'已删除' if gone else '仍在（需手工处理）'}")
        except UgosError as exc:
            print(f"清理失败 {exc.code} {exc.msg}")
    elif hit:
        print(f"保留测试文件：{hit['path']}")

    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
