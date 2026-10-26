#!/usr/bin/env python3
"""下载中心接口探测（只读，不增删任务）。

下载中心是 NAS 最核心的用途之一，但 App 之前完全没接。
这里先确认「读」这一半能不能跑通：任务列表、已完成、速度、设置。

用法: python scripts/probe_downloads.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nas_login_test import load_env  # noqa: E402

from app.ugos.client import UgosClient, UgosError  # noqa: E402

PROBE_DIR = os.path.join(ROOT, ".probe")


def dump(name, payload):
    path = os.path.join(PROBE_DIR, f"dl_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def brief(value, limit=320):
    text = json.dumps(value, ensure_ascii=False)
    return text[:limit] + ("…" if len(text) > limit else "")


async def try_get(client, label, path, params=None):
    try:
        data = await client.get(path, params)
    except UgosError as exc:
        print(f"  [x] {label:28s} code={exc.code} {exc.msg}")
        return None
    print(f"  [ok] {label:28s} {brief(data)}")
    return data


async def main() -> int:
    env = load_env()
    client = UgosClient(
        base=f"https://{env['UGOS_HOST']}:{int(env.get('UGOS_PORT', 9999))}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    await client.login()

    print("[1] 初始化状态 / 基础信息")
    for label, path in [
        ("isInit", "v1/downloadCenter/setup/isInit"),
        ("getPath", "v1/downloadCenter/download/getPath"),
        ("globalSpeed", "v1/downloadCenter/download/globalSpeed"),
    ]:
        data = await try_get(client, label, path)
        if data is not None:
            dump(label, data)

    print("\n[2] 下载任务列表（试几组参数）")
    candidates = [
        {"page": 1, "page_size": 20},
        {"pageNum": 1, "pageSize": 20},
        {"PageNum": 1, "PageSize": 20},
        {"page_num": 1, "page_size": 20},
        {},
    ]
    for path in ("v1/downloadCenter/download/getListV3", "v1/downloadCenter/download/getList"):
        for params in candidates:
            label = f"{path.rsplit('/', 1)[-1]}{list(params.keys()) or '无参'}"
            data = await try_get(client, label, path, params)
            if data is not None:
                dump(label.replace("[", "").replace("]", ""), data)
                break

    print("\n[3] 已完成 / 做种")
    for path in ("v1/downloadCenter/complete/getListV2", "v1/downloadCenter/complete/getList"):
        for params in ({"page": 1, "page_size": 20}, {"PageNum": 1, "PageSize": 20}, {}):
            label = f"{path.rsplit('/', 1)[-1]}{list(params.keys()) or '无参'}"
            data = await try_get(client, label, path, params)
            if data is not None:
                dump(label.replace("[", "").replace("]", ""), data)
                break

    print("\n[4] 设置")
    for label, path in [
        ("general", "v1/downloadCenter/setup/getGeneralSettings"),
        ("btpt", "v1/downloadCenter/setup/getBtPtSettings"),
        ("httpftp", "v1/downloadCenter/setup/getHttpFtpSettings"),
        ("trackers", "v1/downloadCenter/setup/getDefaultTrackerServers"),
    ]:
        data = await try_get(client, label, path)
        if data is not None:
            dump(f"settings_{label}", data)

    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
