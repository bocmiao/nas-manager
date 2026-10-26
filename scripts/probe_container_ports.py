#!/usr/bin/env python3
"""容器端口映射 + 访问链接探测（只读，不改任何容器状态）。

目的：回答「App 内能否直接打开 Docker 应用」的前置技术问题——
UGOS 到底有没有暴露「宿主机端口 → 容器 Web UI」的可达信息。

探测面：
  1. ContainerListV2 / ShowContainerDetailListV2 是否带 portMapping
  2. GetContainerById 的完整原始返回（端口、网络、HostConfig 相关字段）
  3. GetContainerAccessLinkInfo?port=<宿主机端口> 的返回结构
  4. ObtainOverviewInfo 是否附带端口信息

全部为 GET/POST 读接口，不调用 Start/Stop/Remove/Create。
结果写 .probe/container_ports_*.json（.probe/ 已 gitignore）。

用法: python scripts/probe_container_ports.py
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
    path = os.path.join(PROBE_DIR, f"container_ports_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"    -> 写入 {os.path.basename(path)}")


def keys_of(obj) -> list[str]:
    return sorted(obj.keys()) if isinstance(obj, dict) else []


async def main() -> int:
    env = load_env()
    host = env["UGOS_HOST"]
    port = int(env.get("UGOS_PORT", 9999))
    client = UgosClient(
        base=f"https://{host}:{port}",
        username=env["UGOS_USER"],
        password=env["UGOS_PASSWORD"],
    )
    await client.login()
    print(f"登录成功：{host}\n")

    # --- 1. 列表接口是否带端口 ---
    print("[1] ContainerListV2")
    listing = await client.post(
        "v1/docker/container/ContainerListV2", {"PageNum": 1, "PageSize": 500}
    ) or {}
    rows = listing.get("result") or []
    print(f"    容器数={len(rows)}  result 顶层字段={keys_of(rows[0]) if rows else '空'}")
    dump("list", listing)

    print("[2] ShowContainerDetailListV2")
    detail_list = await client.post(
        "v1/docker/container/ShowContainerDetailListV2", {"PageNum": 1, "PageSize": 500}
    ) or {}
    drows = detail_list.get("result") or []
    print(f"    明细数={len(drows)}  顶层字段={keys_of(drows[0]) if drows else '空'}")
    if drows:
        sample = drows[0]
        print(f"    portMapping 存在={('portMapping' in sample)}")
        if sample.get("portMapping"):
            print(f"    portMapping 样例={json.dumps(sample['portMapping'][:3], ensure_ascii=False)}")
    dump("detail_list", detail_list)

    # --- 2. 逐容器取详情，收集端口 ---
    print("[3] GetContainerById（逐容器，取 portMapping + 网络）")
    all_ports: list[tuple[str, str, int, str]] = []  # (name, cid, nasPort, type)
    details: dict[str, dict] = {}
    for row in rows[:30]:
        cid = row.get("containerId") or ""
        name = row.get("name") or ""
        if not cid:
            continue
        try:
            d = await client.get(
                "v1/docker/container/GetContainerById", {"containerId": cid}
            ) or {}
        except UgosError as exc:
            print(f"    {name}: 失败 code={exc.code} {exc.msg}")
            continue
        details[name] = d
        pm = d.get("portMapping") or []
        ports = [p.get("nasPort") for p in pm if isinstance(p, dict) and p.get("nasPort")]
        print(f"    {name:28s} 端口={ports if ports else '无'}")
        for p in pm:
            if isinstance(p, dict) and p.get("nasPort"):
                all_ports.append((name, cid, p["nasPort"], p.get("portType") or ""))
    dump("details", details)

    if details:
        sample_name = next(iter(details))
        print(f"\n    详情接口原始字段（{sample_name}）:")
        print(f"    {keys_of(details[sample_name])}")

    # --- 3. 访问链接接口 ---
    print(f"\n[4] GetContainerAccessLinkInfo（共 {len(all_ports)} 个宿主端口待试）")
    link_results = {}
    for name, cid, port, ptype in all_ports[:20]:
        try:
            data = await client.get(
                "v1/docker/container/GetContainerAccessLinkInfo", {"port": port}
            )
        except UgosError as exc:
            link_results[f"{name}:{port}"] = {"error": f"code={exc.code} {exc.msg}"}
            print(f"    {name}:{port} -> 失败 code={exc.code} {exc.msg}")
            continue
        link_results[f"{name}:{port}"] = data
        text = json.dumps(data, ensure_ascii=False)[:220]
        print(f"    {name}:{port} -> {text}")
    dump("access_link", link_results)

    # --- 4. 概览是否带端口 ---
    print("\n[5] ObtainOverviewInfo")
    try:
        ov = await client.post("v1/docker/view/ObtainOverviewInfo", {}) or {}
        ocs = ov.get("overviewContainers") or []
        print(f"    overviewContainers={len(ocs)}  字段={keys_of(ocs[0]) if ocs else '空'}")
        dump("overview", ov)
    except UgosError as exc:
        print(f"    失败 code={exc.code} {exc.msg}")

    # --- 汇总 ---
    print("\n===== 结论汇总 =====")
    print(f"有端口映射的容器数：{len({p[0] for p in all_ports})} / {len(rows)}")
    print(f"宿主端口总数：{len(all_ports)}")
    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
