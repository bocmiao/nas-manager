#!/usr/bin/env python3
"""缩略图 / 原图预览通道的机械验证（假客户端，不碰 NAS）。

真机契约由 `scripts/probe_media_preview.py` 验证，结论是：
- `thumbnail` 的 `width`/`height` **被 NAS 完全忽略**，恒定回 112×96 JPEG
- `mediaPlayer` **不需要 dl_token**，直接回源文件字节

这里锁住衍生出来的几件事：
1. 两个接口都把 token 塞进 query（不带就是 1010）；
2. 423 要退避重试，其它非 200 直接报错；
3. `preview` 走的是 `mediaPlayer`，不是缩略图，也不是 downloadFile；
4. 路径安全校验在发请求**之前**生效。

用法: python scripts/test_media.py
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from app.config import Settings  # noqa: E402
from app.services.files import FileRejected, FileService  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))


class FakeStream:
    """模拟 httpx 的 stream context：只提供被测代码用到的三个成员。"""

    def __init__(self, status: int, body: bytes, ctype: str = "image/jpeg") -> None:
        self.status_code = status
        self._body = body
        self.headers = {"content-type": ctype}

    async def aread(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class FakeClient:
    """按顺序吐出预置响应，并记录每次调用。"""

    def __init__(self, script: list[FakeStream]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def session_token(self) -> str:
        return "tok-abc"

    def open_stream(self, path: str, params=None):
        self.calls.append((path, dict(params or {})))
        if not self.script:
            return FakeStream(500, b"no more scripted responses")
        return self.script.pop(0)


class FakeNas:
    def __init__(self, client: FakeClient) -> None:
        self.client = client


class FakeCache:
    async def get_or_set(self, _key, _ttl, fn):
        return await fn()


def svc(script: list[FakeStream]) -> tuple[FileService, FakeClient]:
    c = FakeClient(script)
    return FileService(FakeNas(c), FakeCache(), Settings()), c


JPEG = b"\xff\xd8" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128


# ---------------------------------------------------------------- 1. 缩略图

def test_thumbnail() -> None:
    print("\n[1] thumbnail 通道")

    async def ok():
        s, c = svc([FakeStream(200, JPEG)])
        data, ctype = await s.thumbnail(
            "/volume1/docker/a.png", file_type=1, mtime=11, ctime=12, file_size=33
        )
        return data, ctype, c

    data, ctype, c = asyncio.run(ok())
    check("返回二进制原样透出", data == JPEG)
    check("content-type 用 NAS 回的", ctype == "image/jpeg", ctype)
    path, params = c.calls[0]
    check("走的是 v1/filemgr/thumbnail", path == "v1/filemgr/thumbnail", path)
    check("token 在 query 里", params.get("token") == "tok-abc")
    check("path 已传入", params.get("path") == "/volume1/docker/a.png", str(params.get("path")))
    check("size_type 固定 0", params.get("size_type") == 0, str(params.get("size_type")))
    check("type 透传条目值（不硬编码）", params.get("type") == 1, str(params.get("type")))
    check("mtime/ctime/size 都带上（缓存键）",
          (params.get("mtime"), params.get("ctime"), params.get("file_size")) == (11, 12, 33),
          str(params))

    async def retry():
        s, c = svc([
            FakeStream(423, b"thumbnail is generating asynchronizely"),
            FakeStream(423, b"still generating"),
            FakeStream(200, JPEG),
        ])
        return await s.thumbnail("/volume1/docker/a.png", delay=0), c

    (data, ctype), c = asyncio.run(retry())
    check("423 会重试到成功", data == JPEG)
    check("一共打了 3 次", len(c.calls) == 3, str(len(c.calls)))

    async def hard_fail():
        s, _ = svc([FakeStream(404, b"not found")])
        try:
            await s.thumbnail("/volume1/docker/a.png", delay=0)
            return "no-raise"
        except FileRejected as exc:
            return f"rejected:{exc.msg}"

    r = asyncio.run(hard_fail())
    check("非 423 的失败不重试直接报错", r.startswith("rejected:"), r)

    async def exhausted():
        s, _ = svc([FakeStream(423, b"busy")] * 3)
        try:
            await s.thumbnail("/volume1/docker/a.png", retries=3, delay=0)
            return "no-raise"
        except FileRejected as exc:
            return f"rejected:{exc.msg}"

    r = asyncio.run(exhausted())
    check("重试耗尽后报「仍在生成」", "仍在生成" in r, r)


# ---------------------------------------------------------------- 2. 预览

def test_preview() -> None:
    print("\n[2] 原图预览通道")

    async def ok():
        s, c = svc([FakeStream(200, PNG, "image/png")])
        data, ctype = await s.preview(
            "/volume1/docker/a.png", file_type=1, mtime=11, ctime=12, file_size=33
        )
        return data, ctype, c

    data, ctype, c = asyncio.run(ok())
    check("返回的是源文件字节", data == PNG)
    check("content-type 照实回", ctype == "image/png", ctype)
    path, params = c.calls[0]
    check("走的是 mediaPlayer", path == "v1/filemgr/mediaPlayer", path)
    check("不是缩略图接口", "thumbnail" not in path)
    check("token 在 query 里", params.get("token") == "tok-abc")
    check("不带 dl_token（一步取到）", "dl_token" not in params)

    async def empty():
        s, _ = svc([FakeStream(200, b"")])
        try:
            await s.preview("/volume1/docker/a.png")
            return "no-raise"
        except FileRejected as exc:
            return f"rejected:{exc.msg}"

    r = asyncio.run(empty())
    check("空内容报错", r.startswith("rejected:"), r)

    async def denied():
        s, _ = svc([FakeStream(403, b"forbidden")])
        try:
            await s.preview("/volume1/docker/a.png")
            return "no-raise"
        except FileRejected as exc:
            return f"rejected:{exc.msg}"

    r = asyncio.run(denied())
    check("非 200 报错带状态码", "403" in r, r)


# ---------------------------------------------------------------- 3. 路径安全

def test_path_guard() -> None:
    print("\n[3] 路径安全（发请求之前拦掉）")

    async def run():
        s, c = svc([FakeStream(200, JPEG), FakeStream(200, PNG)])
        leaked: list[str] = []
        for bad in ("/volume1/../etc/passwd", "volume1/docker", "/"):
            try:
                await s.thumbnail(bad, delay=0)
                leaked.append(f"thumbnail:{bad}")
            except FileRejected:
                pass
            try:
                await s.preview(bad)
                leaked.append(f"preview:{bad}")
            except FileRejected:
                pass
        return leaked, len(c.calls)

    leaked, calls = asyncio.run(run())
    check("非法路径全部本地拒绝", not leaked, str(leaked))
    check("一次请求都没发出去", calls == 0, f"发了 {calls} 次")


def main() -> int:
    test_thumbnail()
    test_preview()
    test_path_guard()
    failed = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 50)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for _, name, detail in failed:
        print(f"  ! {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
