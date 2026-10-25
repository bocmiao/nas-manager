#!/usr/bin/env python3
"""从 UGOS 主 JS bundle 里提取认证相关代码片段与请求头。"""
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else ".probe/main.js"
src = open(path, encoding="utf-8", errors="ignore").read()
print(f"bundle size: {len(src)}")

print("\n=== ugreen API paths (unique, count) ===")
paths = sorted(set(re.findall(r'["\'`](/?ugreen/v\d+/[A-Za-z0-9_/\-]+)', src)))
print(f"total {len(paths)}")
for p in paths[:60]:
    print(" ", p)

for key in ["verify/check", "verify/login"]:
    print(f"\n=== context: {key} ===")
    for m in list(re.finditer(re.escape(key), src))[:3]:
        s = max(0, m.start() - 420)
        e = min(len(src), m.end() + 420)
        print("---")
        print(src[s:e].replace("\n", " "))

print("\n=== header names ===")
for h in ["x-ugreen-token", "X-Ugreen-Token", "x-ugreen-security-key", "X-Ugreen-Security-Key", "ug-param", "token_id"]:
    hits = len(re.findall(re.escape(h), src, re.I))
    print(f"  {h}: {hits}")

print("\n=== crypto hints ===")
for kw in ["JSEncrypt", "setPublicKey", "RSA", "PKCS1", "aes-", "AES-GCM", "GCM", "createCipheriv", "crypto.subtle", "encrypt("]:
    print(f"  {kw}: {len(re.findall(re.escape(kw), src))}")
