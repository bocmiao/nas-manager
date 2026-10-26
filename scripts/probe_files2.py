#!/usr/bin/env python3
"""文件管理接口探测（第二轮）：路径级列表 + 下载令牌链路 + 文件类型判定。

结果写到 .probe/files2_*.json（已 gitignore）。
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
os.makedirs(PROBE_DIR, exist_ok=True)


def dump(name: str, payload) -> None:
    path = os.path.join(PROBE_DIR, f"files2_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"  -> {path}")


async def post(c: UgosClient, path: str, body: dict) -> dict:
    try:
        return {"ok": True, "data": await c.post(path, body)}
    except UgosError as exc:
        return {"ok": False, "code": exc.code, "msg": exc.msg}


async def get(c: UgosClient, path: str, params: dict | None = None) -> dict:
    try:
        return {"ok": True, "data": await c.get(path, params)}
    except UgosError as exc:
        return {"ok": False, "code": exc.code, "msg": exc.msg}


def list_body(path: str | None, root_type: int | None = None, page: int = 1, limit: int = 50) -> dict:
    b = {
        "limit": limit, "page": page, "is_shield_recycle": False,
        "data_type": 2, "left_no_page_show": False, "left_count": 5000,
        "sort_type": 1, "reverse": False, "intranet_share_id": 0,
        "permission": 0, "version": 1, "operate": 0,
    }
    if root_type is not None:
        b["root_type"] = root_type
    if path:
        b["path"] = path
    return b


def show(rec: dict, label: str) -> list:
    if not rec.get("ok"):
        print(f"  ERR {label}: code={rec.get('code')} {rec.get('msg')}")
        return []
    d = rec["data"] or {}
    rf = d.get("right_files") or {}
    files = rf.get("files") or []
    print(f"  OK  {label}: total={rf.get('total')} n={len(files)} top_path={rf.get('top_path')}")
    for f in files[:6]:
        print(f"       file_type={f.get('file_type')} folder?={f.get('file_type') == 1} "
              f"name={f.get('name')} size={f.get('size')} ext={f.get('ext')}")
    return files


async def main() -> None:
    env = load_env()
    c = UgosClient(base=f"https://{env['UGOS_HOST']}:{env['UGOS_PORT']}",
                   username=env["UGOS_USER"], password=env["UGOS_PASSWORD"])
    await c.login()
    print("登录成功")

    # A) 个人文件夹根 —— 家目录路径含账号名，不在脚本里写死，
    #    先用 root_type=2 让 NAS 自己报出 top_path，再按该路径往下钻
    home = await post(c, "v2/filemgr/getDirFileListV2", list_body(None, root_type=2))
    home_files = show(home, "root_type=2 (个人文件夹)")
    home_path = ((home.get("data") or {}).get("right_files") or {}).get("top_path") or ""
    if home_path:
        print(f"  家目录 top_path = {home_path}")
        home = await post(c, "v2/filemgr/getDirFileListV2", list_body(home_path))
        home_files = show(home, f"path={home_path}") or home_files
    dump("home", home)

    # B) 钻进第一个子目录，看真实文件与 file_type
    found_file = None
    for f in home_files:
        if f.get("file_type") == 1:
            sub = await post(c, "v2/filemgr/getDirFileListV2", list_body(f["path"]))
            sub_files = show(sub, f"path={f['path']}")
            dump("sub", sub)
            cand = next((x for x in sub_files if x.get("file_type") != 1), None)
            if cand:
                found_file = cand
                break

    # C) 共享文件夹根（root_type=3，permission 要 4）
    b = list_body(None, root_type=3)
    b["permission"] = 4
    shares = await post(c, "v2/filemgr/getDirFileListV2", b)
    share_files = show(shares, "root_type=3 (共享)")
    dump("shares_root", shares)
    if not found_file:
        for f in share_files:
            if f.get("file_type") != 1:
                found_file = f
                break

    # D) 下载链路
    if found_file:
        print(f"\n下载链路目标: {found_file['name']} ({found_file.get('size')} B)")
        paths = [{"path": found_file["path"],
                  "intranet_share_id": found_file.get("intranet_share_id") or 0}]
        task = await post(c, "v2/filemgr/addPathsTask", {"paths": paths})
        dump("addtask", task)
        uuid = ((task.get("data") or {}).get("result")) if task.get("ok") else None
        print(f"  task uuid={uuid} raw={json.dumps(task, ensure_ascii=False)[:200]}")
        if uuid:
            for coding in ("true", "false"):
                tok = await get(c, "v2/filemgr/getDownloadToken",
                                {"task_uuid": str(uuid), "coding": coding, "is_livephoto": "false"})
                got = ((tok.get("data") or {}).get("dl_token")) if tok.get("ok") else None
                print(f"  coding={coding} dl_token len={len(got or '')} ok={bool(got)}")
                if got:
                    dump("dltoken", tok)
                    print(f"  dl_token 前缀: {got[:16]}...")
                    try:
                        r = await c._http.get("/ugreen/v2/filemgr/downloadFile",
                                              params={"token": got})  # noqa: SLF001
                        body = await r.aread()
                        print(f"  HTTP {r.status_code} content-type={r.headers.get('content-type')} "
                              f"bytes={len(body)} head={body[:80]!r}")
                        if r.status_code == 200 and not body.startswith(b"{"):
                            out = os.path.join(PROBE_DIR, "downloaded_sample.bin")
                            with open(out, "wb") as fp:
                                fp.write(body)
                            print(f"  已保存样本 -> {out}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  下载失败: {type(exc).__name__}: {exc}")
                    break

    # E) 空间查询
    if home_path:
        free = await post(c, "v1/filemgr/getPathFreeSize", {"path": home_path})
    print(f"\n空间: {json.dumps(free, ensure_ascii=False)[:300]}")

    await c.close()
    print("\n完成")


if __name__ == "__main__":
    asyncio.run(main())
