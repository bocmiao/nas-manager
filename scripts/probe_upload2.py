#!/usr/bin/env python3
"""上传协议第二轮：中文文件名 + 多块续传语义。

第一轮已确认（scripts/probe_upload.py）：
- 端点 `POST v1/filemgr/fileUploadV2`，参数全在 header
- `change_time` 必须是 **int64 unix 秒**，传格式化字符串会被 Go 反序列化拒绝
- `uuid` 用文件名 md5 可用，单块（current_size=size, begin_size=0,
  first_request=false）一次写成功

本轮只补两个会影响实现的疑问：
A. 中文文件名 / 中文目录能不能传（header 只能 latin-1，必须 URL 编码）
B. 分块到底是"追加"还是"覆盖"——第二块的 begin_size 必须等于第一块结束位置才能验证

同样会在结束后清理。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nas_login_test import load_env  # noqa: E402

from app.ugos.client import UgosClient, UgosError  # noqa: E402

PROBE_DIR = os.path.join(ROOT, ".probe")
os.makedirs(PROBE_DIR, exist_ok=True)

DEST_DIR = "/volume1/docker/backup"
CN_DIR = "/volume1/docker/_nasmgr_cn"
CN_NAME = "_nasmgr_中文 测试.txt"


def dump(name: str, payload) -> None:
    path = os.path.join(PROBE_DIR, f"upload2_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    print(f"  -> {path}")


def list_body(path: str) -> dict:
    return {"limit": 200, "page": 1, "is_shield_recycle": False, "data_type": 2,
            "left_no_page_show": False, "left_count": 5000, "sort_type": 1,
            "reverse": False, "intranet_share_id": 0, "permission": 0,
            "version": 1, "operate": 0, "path": path}


async def listdir(c: UgosClient, path: str) -> list[dict]:
    try:
        d = await c.post("v2/filemgr/getDirFileListV2", list_body(path))
    except UgosError:
        return []
    return (d.get("right_files") or {}).get("files") or []


async def raw_post(c: UgosClient, path: str, body: bytes, param: dict, filename: str) -> dict:
    tok = await c._ensure_token()  # noqa: SLF001
    headers = {
        "Content-Type": "application/octet-stream",
        # header 只能 latin-1；前端用 encodeURIComponent，这里用 quote(safe="")
        "Content-Disposition": f'attachment; filename="{quote(filename, safe="")}"',
        "ug-param": json.dumps(param, ensure_ascii=True, separators=(",", ":")),
    }
    r = await c._http.post(c._url(path), content=body,  # noqa: SLF001
                           params={"token": tok}, headers=headers)
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {"_raw": r.text[:300], "_status": r.status_code}


def param(uid: str, name: str, dest: str, size: int, current: int, begin: int,
          first: bool = False) -> dict:
    # `ug-param` 整个要放进 header，而 header 只能 ASCII：
    # 前端对 file_name 和 dir 都做了 encodeURIComponent，这里必须一致，
    # 否则中文名/中文目录会直接 UnicodeEncodeError（或者被 NAS 当乱码）。
    return {
        "uuid": uid, "file_name": quote(name, safe=""), "action_type": 1,
        "size": size, "current_size": current, "resume": True,
        "dir": quote(dest, safe=""), "change_time": int(time.time()),
        "is_live_photo": False, "first_request": first, "begin_size": begin,
    }


async def cleanup(c: UgosClient, paths: list[str]) -> None:
    if not paths:
        return
    try:
        await c.post("v2/filemgr/delPaths", {"paths": paths, "forever": True})
        print(f"  清理：已删除 {paths}")
    except UgosError as exc:
        print(f"  清理失败 {exc.code} {exc.msg} → {paths}")


async def main() -> None:
    env = load_env()
    c = UgosClient(base=f"https://{env['UGOS_HOST']}:{env['UGOS_PORT']}",
                   username=env["UGOS_USER"], password=env["UGOS_PASSWORD"])
    await c.login()
    print("登录成功")
    to_clean: list[str] = []

    # ---------------- A. 中文文件名 + 中文目录 ----------------
    print("\n[A] 中文目录 + 中文（含空格）文件名")
    try:
        r = await c.post("v2/filemgr/createFolder", {"path": CN_DIR})
        print(f"  建中文目录 -> {json.dumps(r, ensure_ascii=False)[:120]}")
        to_clean.append(CN_DIR)
    except UgosError as exc:
        print(f"  建中文目录 {exc.code} {exc.msg}（可能已存在，继续）")
        to_clean.append(CN_DIR)

    payload = "中文内容测试\n".encode()
    uid = hashlib.md5(CN_NAME.encode()).hexdigest()
    res = await raw_post(c, "v1/filemgr/fileUploadV2", payload,
                         param(uid, CN_NAME, CN_DIR, len(payload), len(payload), 0),
                         CN_NAME)
    dump("cn", res)
    print(f"  上传 -> {json.dumps(res, ensure_ascii=False)[:200]}")
    files = await listdir(c, CN_DIR)
    hit = next((f for f in files if f.get("name") == CN_NAME), None)
    print(f"  目录校验：{'出现 ' + str(hit.get('size')) + ' 字节' if hit else '未出现'}")
    print(f"  中文名可上传：{bool(hit)}")

    # ---------------- B. 分块语义 ----------------
    print("\n[B] 分块：两段写入，验证 begin_size 是追加偏移还是覆盖")
    name = "_nasmgr_chunk_probe.txt"
    part1 = b"A" * 400
    part2 = b"B" * 400
    total = len(part1) + len(part2)
    uid2 = hashlib.md5(name.encode()).hexdigest()

    r1 = await raw_post(c, "v1/filemgr/fileUploadV2", part1,
                        param(uid2, name, DEST_DIR, total, len(part1), 0), name)
    print(f"  第 1 块(400B) -> {json.dumps(r1, ensure_ascii=False)[:160]}")
    r2 = await raw_post(c, "v1/filemgr/fileUploadV2", part2,
                        param(uid2, name, DEST_DIR, total, len(part2), len(part1)), name)
    print(f"  第 2 块(400B,begin=400) -> {json.dumps(r2, ensure_ascii=False)[:160]}")
    dump("chunk", {"p1": r1, "p2": r2})

    files = await listdir(c, DEST_DIR)
    hit2 = next((f for f in files if f.get("name") == name), None)
    print(f"  目录校验：{'size=' + str(hit2.get('size')) if hit2 else '未出现'}")
    if hit2:
        to_clean.append(hit2["path"])
        # 读回来比对内容，确认是拼接而不是覆盖
        try:
            task = await c.post("v2/filemgr/addPathsTask",
                                {"paths": [{"path": hit2["path"], "intranet_share_id": 0}]})
            tu = (task or {}).get("result")
            tok = await c.get("v2/filemgr/getDownloadToken",
                              {"task_uuid": str(tu), "coding": "true", "is_livephoto": "false"})
            dl = (tok or {}).get("dl_token")
            resp = await c._http.get("/ugreen/v2/filemgr/downloadFile",  # noqa: SLF001
                                     params={"token": dl})
            got = await resp.aread()
            print(f"  读回 {len(got)} 字节，前 8={got[:8]!r} 后 8={got[-8:]!r}")
            if len(got) == total:
                head_a = got[:len(part1)] == part1
                tail_b = got[len(part1):] == part2
                print(f"  拼接结果：前半是 A？{head_a} 后半是 B？{tail_b}")
            else:
                print("  长度不符——分块不是简单追加，需要继续验证")
        except UgosError as exc:
            print(f"  读回失败 {exc.code} {exc.msg}")

    # ---------------- 清理 ----------------
    print("\n[清理]")
    await cleanup(c, to_clean)
    left = await listdir(c, DEST_DIR)
    print(f"  目标目录剩余：{[f.get('name') for f in left]}")
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
