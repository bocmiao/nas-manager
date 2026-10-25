#!/usr/bin/env python3
"""登录后逐个探测端点，确定真实可用的方法与返回结构。

用法: python scripts/probe_endpoints.py [端点...]（缺省用内置候选清单）
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nas_login_test import UgosClient, load_env  # noqa: E402

CANDIDATES = [
    "v1/sysinfo/machine/common",
    "v1/sysinfo/finder/get",
    "v1/user/current/user",
    "v1/verify/is_login",
    "v1/verify/is_running",
    "v1/verify/sn",
    "v1/desktop/components",
    "v2/desktop/components",
    "v1/desktop/module/list",
    "v1/app/info",
    "v1/idx/info",
    "v1/language/global",
    "v1/firmware/version/is_new",
    "v1/firmware/status",
    "v1/log/query",
    "v1/filemgr/getDirFileList",
    "v1/filemgr/getHomeFolderSet",
    "v1/time/config",
    "v1/user/config",
    "v1/user/config/global",
]


def brief(data) -> str:
    if not isinstance(data, dict):
        return str(data)[:150]
    if "error" in data:
        return "body=" + str(data["error"]).replace("\n", " ")[:150]
    d = data.get("data")
    if isinstance(d, dict):
        keys = list(d.keys())[:10]
        return f"data.keys={keys}"
    if isinstance(d, list):
        return f"data=list[{len(d)}]"
    return f"data={str(d)[:100]}"


def main() -> int:
    env = load_env()
    endpoints = sys.argv[1:] or CANDIDATES
    host, port = env.get("UGOS_HOST", "192.0.2.10"), int(env.get("UGOS_PORT", "9443"))
    c = UgosClient(host, port)
    info = c.login(env["UGOS_USER"], env["UGOS_PASSWORD"])
    if not info["has_token"]:
        print(f"login failed: {info}")
        return 1
    print(f"login ok, token len={len(c.token)}\n")

    sec = hashlib.md5(c.token.encode()).hexdigest()
    hdr = {"X-Ugreen-Security-Key": sec}
    hits = []
    for ep in endpoints:
        for method in ("GET", "POST"):
            params = {"token": c.token}
            if method == "GET":
                st, _, data = c.get(ep, params, hdr)
            else:
                st, _, data = c.post(f"{ep}?token={c.token}", {}, hdr)
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg") if isinstance(data, dict) else None
            ok = code == 200
            flag = "OK " if ok else "   "
            print(f"{flag}{method:4s} {ep} -> http={st} code={code} msg={msg} {brief(data)}")
            if ok:
                hits.append((method, ep, data))
                break
    print(f"\nusable: {len(hits)}/{len(endpoints)}")
    if hits:
        with open(".probe/probe_result.json", "w", encoding="utf-8") as f:
            json.dump([{"method": m, "endpoint": e, "data": d} for m, e, d in hits],
                      f, ensure_ascii=False, indent=1)
        print("detail saved -> .probe/probe_result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
