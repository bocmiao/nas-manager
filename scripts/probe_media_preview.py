#!/usr/bin/env python3
"""确认大图预览走哪条路。

逆向前端发现两件事，之前都搞错了：
1. 列表缩略图用的是 `width/height` **固定预设**（`[16,120,88,48]`），
   不是随便传多大就出多大 —— 之前传 1024 回了个 112x96 的怪尺寸。
2. filemgr 里还有一个 `mediaPlayer` 端点，参数跟 thumbnail 几乎一样，
   多一个 `code=1`，推测是「原图/播放」通道。

这个脚本自己造一张 1600x1200 PNG 上传，分别试：
  A. thumbnail + 各预设宽度，看真实回图尺寸
  B. mediaPlayer（带/不带 code），看是二进制还是 JSON

测完删除测试文件，不碰别人的数据。
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
    "limit": 200, "page": 1, "dir_recycle": False, "data_type": 2,
    "left_no_page_show": False, "left_count": 5000, "sort_type": 1,
    "reverse": False, "version": 1, "operate": 0, "intranet_share_id": 0,
    "permission": 0, "is_shield_recycle": False,
}
NAME = "_nasmgr_preview_probe.png"


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


def png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def describe(buf: bytes) -> str:
    """给返回体一个人类可读的身份。"""
    dims = jpeg_size(buf) or png_size(buf)
    kind = "JPEG" if jpeg_size(buf) else ("PNG" if png_size(buf) else "非图片")
    if kind == "非图片":
        head = buf[:200].decode("utf-8", "replace").replace("\n", " ")
        return f"非图片 len={len(buf)} head={head!r}"
    return f"{kind} {dims} len={len(buf)}"


def make_png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    rows = []
    for _y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            v = int(255 * x / max(1, width - 1))
            row += bytes((v, 40, 255 - v))
        rows.append(bytes(row))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + chunk(b"IEND", b"")
    )


async def writable_dir(client: UgosClient) -> str | None:
    data = await client.post(
        "v2/filemgr/getDirFileListV2", dict(BASE, root_type=3, permission=4)
    ) or {}
    for f in (data.get("right_files") or {}).get("files") or []:
        if f.get("file_type") == 1 and f.get("path"):
            return f["path"]
    return None


async def upload(client: UgosClient, directory: str, payload: bytes) -> None:
    param = {
        "uuid": hashlib.md5(NAME.encode()).hexdigest(),
        "file_name": NAME,
        "action_type": 3,
        "size": len(payload),
        "current_size": len(payload),
        "resume": True,
        "dir": directory.replace("/", "%2F"),
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
    await client.post_raw("v1/filemgr/fileUploadV2", payload, headers=headers)


async def fetch(client, endpoint: str, params: dict, attempts: int = 8) -> tuple[int, bytes]:
    """取一次，423 就退避重试。"""
    for attempt in range(1, attempts + 1):
        buf = b""
        async with client.open_stream(endpoint, params) as resp:
            async for piece in resp.aiter_bytes(16384):
                buf += piece
            code = resp.status_code
        if code != 423:
            return code, buf
        await asyncio.sleep(1.0)
    return 423, buf


async def main() -> int:
    env = load_env()
    client = UgosClient(
        base=f"https://{env['UGOS_HOST']}:{int(env.get('UGOS_PORT', 9999))}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    token = await client.login()

    directory = await writable_dir(client)
    if not directory:
        print("找不到可写共享目录")
        await client.close()
        return 1

    payload = make_png(1600, 1200)
    print(f"[1] 上传 1600x1200 测试图 {len(payload)} 字节 → {directory}")
    try:
        await upload(client, directory, payload)
    except UgosError as exc:
        print(f"    [x] 上传失败 {exc.code} {exc.msg}")
        await client.close()
        return 1

    entry = None
    for _ in range(6):
        data = await client.post(
            "v2/filemgr/getDirFileListV2", dict(BASE, path=directory)
        ) or {}
        for f in (data.get("right_files") or {}).get("files") or []:
            if f.get("name") == NAME:
                entry = f
                break
        if entry:
            break
        await asyncio.sleep(1)
    if not entry:
        print("    [x] 列表里没看到刚上传的文件")
        await client.close()
        return 1
    print(f"    条目 type={entry.get('type')} ext={entry.get('ext')} "
          f"ext_group={entry.get('ext_group')}")

    base_params = {
        "path": entry.get("path"),
        "type": entry.get("type") or 1,
        "size_type": 0,
        "mtime": entry.get("mtime"),
        "ctime": entry.get("ctime"),
        "file_size": entry.get("size"),
        "intranet_share_id": entry.get("intranet_share_id") or 0,
    }

    print("\n[2] thumbnail：前端的预设宽度")
    try:
        for side in (16, 48, 88, 120, 256, 512):
            code, buf = await fetch(
                client,
                "v1/filemgr/thumbnail",
                {**base_params, "width": side, "height": side, "token": token},
            )
            print(f"  width={side:>4} size_type=0 → HTTP {code} {describe(buf)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  thumbnail 异常 {type(exc).__name__}: {exc}")

    print("\n[3] mediaPlayer：带 code=1")
    for label, extra in (("code=1", {"code": 1}), ("无 code", {})):
        try:
            code, buf = await fetch(
                client,
                "v1/filemgr/mediaPlayer",
                {**base_params, **extra, "width": 1920, "height": 1080, "token": token},
            )
            print(f"  [{label}] HTTP {code} {describe(buf)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{label}] {type(exc).__name__}: {exc}")

    print("\n[4] 清理")
    try:
        await client.post(
            "v2/filemgr/delPaths",
            {"paths": [f"{directory}/{NAME}"], "forever": True},
        )
        print("    已删除测试图片")
    except UgosError as exc:
        print(f"    [x] 清理失败 {exc.code} {exc.msg} —— 请手动删除 {NAME}")

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
