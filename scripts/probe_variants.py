#!/usr/bin/env python3
"""定位 token 的正确传递方式：对同一端点测试多种组合。"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nas_login_test import UgosClient, load_env  # noqa: E402

ENDPOINT = "ugreen/v1/idx/info"


def call(base: str, opener, method: str, path: str, body: dict | None,
         headers: dict[str, str]) -> tuple[int, str]:
    url = f"{base}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=15) as r:
            return r.status, r.read()[:200].decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "ignore")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def main() -> int:
    env = load_env()
    host, port = env.get("UGOS_HOST", "192.0.2.10"), int(env.get("UGOS_PORT", "9443"))
    c = UgosClient(host, port)
    info = c.login(env["UGOS_USER"], env["UGOS_PASSWORD"])
    if not info["has_token"]:
        print(f"login failed: {info}")
        return 1
    tok = c.token
    sec = hashlib.md5(tok.encode()).hexdigest()
    base = f"https://{host}:{port}"
    print(f"login ok  token={tok[:8]}...({len(tok)})  cookies={[x.name for x in c.jar]}\n")

    common = {"Content-Type": "application/json", "UG-Agent": "PC/WEB",
              "Client-Version": "78471", "Client-Version-Str": "1.19.0",
              "X-Specify-Language": "zh-CN"}
    q = urllib.parse.urlencode({"token": tok})

    variants: list[tuple[str, str, str, dict | None, dict[str, str]]] = [
        ("A 无 token", "POST", ENDPOINT, {}, common),
        ("B query token", "POST", f"{ENDPOINT}?{q}", {}, common),
        ("C query token(未编码)", "POST", f"{ENDPOINT}?token={tok}", {}, common),
        ("D header Token", "POST", ENDPOINT, {}, {**common, "X-Ugreen-Token": tok}),
        ("E header Security-Key", "POST", ENDPOINT, {}, {**common, "X-Ugreen-Security-Key": sec}),
        ("F query + Security-Key", "POST", f"{ENDPOINT}?{q}", {}, {**common, "X-Ugreen-Security-Key": sec}),
        ("G GET 无 token", "GET", ENDPOINT, None, common),
        ("H GET + query token", "GET", f"{ENDPOINT}?{q}", None, common),
        ("I GET + query + SecKey", "GET", f"{ENDPOINT}?{q}", None, {**common, "X-Ugreen-Security-Key": sec}),
    ]

    for label, method, path, body, hdrs in variants:
        st, txt = call(base, c.opener, method, path, body, hdrs)
        print(f"{label:24s} {method:4s} -> {st}  {txt[:140]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
