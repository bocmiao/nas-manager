#!/usr/bin/env python3
"""暴力探测候选端点是否存在。

判定依据：路由不存在时 UGOS 返回 HTML/404（客户端报「非 JSON 响应」），
路由存在但参数不对时返回 JSON + code 9999/1005，两者可区分。

用法: python scripts/probe_candidates.py            # 跑内置候选表
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

CANDIDATES: list[tuple[str, str, dict | None]] = [
    # 快照：folder 维度（bundle 里确认存在 v1/snapshot/folder/{type}）
    ("get", "v1/snapshot/folder/share", None),
    ("get", "v1/snapshot/folder/volume", None),
    ("get", "v1/snapshot/folder/home", None),
    ("get", "v1/snapshot/folder/all", None),
    ("get", "v1/snapshot/folder/user", None),
    ("get", "v1/snapshot/folder", None),
    # 快照：版本 / 管理
    ("get", "v1/snapshot/list", {"page": 1, "size": 20}),
    ("get", "v1/snapshot/version", {"page": 1, "size": 20}),
    ("get", "v1/snapshot/version/list", {"page": 1, "size": 20}),
    ("get", "v1/snapshot/manage/list", {"page": 1, "size": 20}),
    ("get", "v1/snapshot/task", None),
    ("get", "v1/snapshot/preference/web_settings", None),
    ("get", "v1/snapshot/data/getFolderList", None),
    # 存储域下的快照
    ("get", "v1/storage/snapshot/list", {"page": 1, "size": 20}),
    ("get", "v2/storage/snapshot/list", {"page": 1, "size": 20}),
    ("get", "v1/storage/volume/snapshot/list", {"page": 1, "size": 20}),
    ("get", "v1/storage/snapshot", None),
    # 文件版本（com.ugreen.versionmgr）
    ("get", "v1/version/list", {"page": 1, "size": 20}),
]


async def main() -> int:
    env = load_env()
    base = f"https://{env.get('UGOS_HOST', '192.0.2.10')}:{env.get('UGOS_PORT', '9443')}"
    c = UgosClient(base, env.get("UGOS_USER", ""), env.get("UGOS_PASSWORD", ""))
    rows = []
    try:
        await c.login()
        for method, path, params in CANDIDATES:
            rec = {"method": method, "path": path, "params": params}
            try:
                data = (await c.get(path, params) if method == "get"
                        else await c.post(path, params))
                rec.update(exists=True, code=200, data=data)
                print(f"  200  {method.upper():4s} {path}  data={str(data)[:120]}")
            except UgosError as exc:
                exists = not str(exc.msg).startswith("HTTP 4") and exc.code != -1
                rec.update(exists=exists, code=exc.code, msg=exc.msg, debug=exc.debug)
                print(f"  {exc.code:>4} {method.upper():4s} {path}  "
                      f"exists={exists} {exc.msg} {exc.debug}")
            rows.append(rec)
    finally:
        await c.close()
    out = os.path.join(ROOT, ".probe", "batch2_candidates.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
