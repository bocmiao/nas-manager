#!/usr/bin/env python3
"""缩略图能力验证：自己造一张 PNG 上传，测完删除。

文件列表里所有图片现在只显示一个通用图标，看不出内容。
缩略图接口是 `GET v1/filemgr/thumbnail?...&width=&height=`，返回图片二进制。
这里确认它到底能不能取到真图、鉴权方式是什么。

只碰自己创建的测试文件，测完清理。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import sys
import time
import zlib

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
NAME = "_nasmgr_thumb_probe.png"


def make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """手搓一张纯色 PNG，避免引入 Pillow 依赖。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def main() -> int:
    env = load_env()
    client = UgosClient(
        base=f"https://{env['UGOS_HOST']}:{int(env.get('UGOS_PORT', 9999))}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    token = await client.login()

    # 找一个可写共享目录
    body = dict(BASE, root_type=3, permission=4)
    data = await client.post("v2/filemgr/getDirFileListV2", body) or {}
    base_dir = None
    for f in (data.get("right_files") or {}).get("files") or []:
        if f.get("file_type") == 1 and f.get("path"):
            base_dir = f["path"]
            break
    if not base_dir:
        print("找不到可写目录")
        await client.close()
        return 1
    print(f"目标目录: {base_dir}")

    png = make_png(64, 64, (200, 60, 60))
    print(f"\n[1] 上传测试图片 {len(png)} 字节")
    param = {
        "uuid": hashlib.md5(NAME.encode()).hexdigest(),
        "file_name": NAME,
        "action_type": 3,
        "size": len(png),
        "current_size": len(png),
        "resume": True,
        "dir": base_dir.replace("/", "%2F"),
        "change_time": int(time.time()),
        "is_live_photo": False,
        "first_request": False,
        "begin_size": 0,
    }
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="{NAME}"',
        "ug-param": json.dumps(param, ensure_ascii=True, separators=(",", ":")),
    }
    try:
        await client.post_raw("v1/filemgr/fileUploadV2", png, headers=headers)
        print("    上传完成")
    except UgosError as exc:
        print(f"    [x] 上传失败 {exc.code} {exc.msg}")
        await client.close()
        return 1

    # 拿到条目的真实字段（缩略图要 mtime/ctime/size）
    data = await client.post("v2/filemgr/getDirFileListV2", dict(BASE, path=base_dir)) or {}
    entry = None
    for f in (data.get("right_files") or {}).get("files") or []:
        if f.get("name") == NAME:
            entry = f
            break
    if not entry:
        print("    [x] 列表里没看到刚上传的文件")
    else:
        print(f"    条目: ext={entry.get('ext')} type={entry.get('type')} "
              f"size={entry.get('size')} mtime={entry.get('mtime')} ctime={entry.get('ctime')}")
        print("\n[2] 请求缩略图")
        params = {
            "path": entry.get("path"),
            "type": entry.get("type") or 2,
            "size_type": 0,
            "mtime": entry.get("mtime") or 0,
            "ctime": entry.get("ctime") or 0,
            "file_size": entry.get("size") or 0,
            "intranet_share_id": entry.get("intranet_share_id") or 0,
            "width": 96,
            "height": 96,
        }
        # 先试带 token（我们的客户端是 token 鉴权，不是 cookie）
        for label, extra in (("带 token", {"token": token}), ("不带 token", {})):
            try:
                async with client.open_stream("v1/filemgr/thumbnail", {**params, **extra}) as resp:
                    head = b""
                    async for piece in resp.aiter_bytes(2048):
                        head += piece
                        if len(head) >= 64:
                            break
                    ctype = resp.headers.get("content-type") or ""
                    is_png = head[:8] == b"\x89PNG\r\n\x1a\n"
                    is_jpeg = head[:2] == b"\xff\xd8"
                    print(f"    [{label}] HTTP {resp.status_code} type={ctype} "
                          f"len={resp.headers.get('content-length')} png={is_png} jpeg={is_jpeg}")
                    if is_png or is_jpeg:
                        print(f"    → 取到真图片了，前16字节={head[:16].hex()}")
                        break
                    else:
                        print(f"    → 不是图片，前 120 字符={head[:120]!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"    [{label}] {type(exc).__name__}: {exc}")

    print("\n[3] 清理")
    try:
        await client.post(
            "v2/filemgr/delPaths",
            {"paths": [f"{base_dir}/{NAME}"], "forever": True},
        )
        print("    已删除测试图片")
    except UgosError as exc:
        print(f"    [x] 清理失败 {exc.code} {exc.msg} —— 请手动删除 {NAME}")

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
