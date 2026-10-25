#!/usr/bin/env python3
"""从 UGOS bundle 提取所有 API 路径。用法: extract_endpoints.py [file] [filter]"""
import json
import re
import sys
from collections import Counter

path = sys.argv[1] if len(sys.argv) > 1 else ".probe/main.js"
flt = sys.argv[2].lower() if len(sys.argv) > 2 else ""
src = open(path, encoding="utf-8", errors="ignore").read()

pat = re.compile(r"""["'`](v[0-9]+/[A-Za-z0-9_/\-.{}:$]+)["'`]""")
found = Counter(m.group(1) for m in pat.finditer(src))

print(f"unique endpoints: {len(found)}")
for ep, n in sorted(found.items()):
    if flt and flt not in ep.lower():
        continue
    print(f"{n:3d}  {ep}")

# 另存一份全量清单，供后续逐个探测
out = ".probe/endpoints.json"
json.dump(sorted(found), open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\nsaved -> {out}")
