#!/usr/bin/env python3
"""列出 NAS 上已安装应用，供「还有哪些能接」的功能盘点用（只读）。"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nas_login_test import load_env  # noqa: E402

from app.ugos.client import UgosClient  # noqa: E402


async def main() -> int:
    env = load_env()
    client = UgosClient(
        base=f"https://{env['UGOS_HOST']}:{int(env.get('UGOS_PORT', 9999))}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    await client.login()
    data = await client.get("v1/desktop/module/list") or {}
    rows = data.get("result") or data.get("list") or []
    print(f"{'module_id':28s} {'entry':16s} {'status':8s} name")
    print("-" * 78)
    for m in rows:
        if not isinstance(m, dict):
            continue
        print(
            f"{str(m.get('module_id')):28s} {str(m.get('entry')):16s} "
            f"{str(m.get('app_status')):8s} {m.get('name')}"
        )
    print(f"\n共 {len(rows)} 个应用")
    with open(os.path.join(ROOT, ".probe", "modules.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
