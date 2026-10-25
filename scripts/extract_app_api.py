#!/usr/bin/env python3
"""从 UGOS 各应用的 SPA bundle 提取 API 端点。

用法: python scripts/extract_app_api.py /taskmgr /storagemgr /docker
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nas_login_test import load_env  # noqa: E402

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

PAT = re.compile(r"""["'`](v[0-9]+/[A-Za-z0-9_/\-.{}$:]+)["'`]""")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=ssl_ctx, timeout=25) as r:
        return r.read()


def js_urls(html: str, entry: str) -> list[str]:
    out = []
    for m in re.finditer(r"""(?:src|href)=["']([^"']+?\.js)["']""", html):
        u = m.group(1)
        if u.startswith("http"):
            out.append(u)
        elif u.startswith("/"):
            out.append(u)
        else:
            out.append(f"{entry.rstrip('/')}/{u.lstrip('./')}")
    return out


def main() -> int:
    env = load_env()
    host, port = env.get("UGOS_HOST", "192.0.2.10"), int(env.get("UGOS_PORT", "9443"))
    base = f"https://{host}:{port}"
    entries = sys.argv[1:] or ["/taskmgr"]

    os.makedirs(".probe/apps", exist_ok=True)
    summary: dict[str, list[str]] = {}

    for entry in entries:
        print(f"\n{'=' * 20} {entry}")
        try:
            html = fetch(f"{base}{entry}/").decode("utf-8", "ignore")
        except Exception as e:
            print(f"  html fetch failed: {type(e).__name__}: {e}")
            continue
        urls = js_urls(html, entry)
        print(f"  js files: {len(urls)}")
        counter: Counter[str] = Counter()
        for u in urls:
            try:
                body = fetch(u if u.startswith("http") else base + u).decode("utf-8", "ignore")
            except Exception as e:
                print(f"  skip {u}: {type(e).__name__}")
                continue
            name = f".probe/apps{entry.replace('/', '_')}_{os.path.basename(u)}"
            open(name, "w", encoding="utf-8").write(body)
            for m in PAT.finditer(body):
                counter[m.group(1)] += 1
        summary[entry] = sorted(counter)
        print(f"  endpoints: {len(counter)}")
        for ep, n in sorted(counter.items()):
            print(f"    {n:3d}  {ep}")

    out = ".probe/app_endpoints.json"
    json.dump(summary, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nsaved -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
