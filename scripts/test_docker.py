#!/usr/bin/env python3
"""测试 Docker 容器列表的请求参数。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nas_login_test import UgosClient, load_env  # noqa: E402


def main() -> int:
    env = load_env()
    host, port = env.get("UGOS_HOST", "192.0.2.10"), int(env.get("UGOS_PORT", "9443"))
    c = UgosClient(host, port)
    assert c.login(env["UGOS_USER"], env["UGOS_PASSWORD"])["has_token"], "login failed"

    bodies = [
        {"PageNum": 1, "PageSize": 100},
        {"PageNum": 1, "PageSize": 100, "Status": "", "Keyword": "", "SortField": "", "SortOrder": ""},
    ]
    for ep in ("v1/docker/container/ContainerListV2", "v1/docker/container/ShowContainerList"):
        for body in bodies:
            st, _, data = c.post(f"{ep}?token={c.token}", body)
            code = data.get("code")
            print(f"{ep} body={list(body.keys())} -> http={st} code={code} msg={data.get('msg')}")
            if code == 200:
                d = data.get("data") or {}
                print("  keys:", list(d.keys()), "total:", d.get("total"), "originalTotal:", d.get("originalTotal"))
                items = d.get("result") or []
                print(f"  containers: {len(items)}")
                for it in items:
                    print(f"   - {it.get('name') or it.get('Name')}  state={it.get('state', it.get('State'))}  image={it.get('image') or it.get('Image')}")
                if items:
                    print("  sample keys:", list(items[0].keys()))
                    print("  sample:", json.dumps(items[0], ensure_ascii=False)[:700])
                return 0
            err = (data.get("debug") or str(data))[:200]
            print("  err:", err)
    return 1


if __name__ == "__main__":
    sys.exit(main())
