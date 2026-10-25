#!/usr/bin/env python3
"""打印 bundle 中指定关键字的上下文片段。用法: ctx.py <file> <kw> [kw...]"""
import re
import sys

path = sys.argv[1]
keys = sys.argv[2:]
src = open(path, encoding="utf-8", errors="ignore").read()

for k in keys:
    print(f"\n{'=' * 20} {k} {'=' * 20}")
    hits = list(re.finditer(re.escape(k), src))
    print(f"hits: {len(hits)}")
    for m in hits[:2]:
        s = max(0, m.start() - 500)
        e = min(len(src), m.end() + 700)
        print("---")
        print(src[s:e].replace("\n", " "))
