#!/usr/bin/env python3
"""UGOS Pro 总览看板 —— MVP 服务端原型。

拉取机器信息、实时监控、磁盘、存储池/卷、容器状态，输出人类可读的看板。
所有数据来自已实测的 UGOS 私有接口。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nas_login_test import UgosClient, load_env  # noqa: E402


def human_bytes(n: float) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024
    return "-"


def human_rate(n: float) -> str:
    return f"{human_bytes(n)}/s"


def human_uptime(sec: int) -> str:
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d} 天 {h} 小时 {m} 分"
    return f"{h} 小时 {m} 分"


def bar(pct: float, width: int = 20) -> str:
    filled = int(round(max(0.0, min(100.0, pct)) / 100 * width))
    return "█" * filled + "·" * (width - filled)


class Nas:
    def __init__(self) -> None:
        env = load_env()
        self.c = UgosClient(env.get("UGOS_HOST", "192.0.2.10"), int(env.get("UGOS_PORT", "9443")))
        info = self.c.login(env["UGOS_USER"], env["UGOS_PASSWORD"])
        if not info["has_token"]:
            raise RuntimeError(f"login failed: {info}")

    def get(self, path: str, params: dict | None = None):
        """GET 是主通道：token 走 query 参数。"""
        p = {"token": self.c.token}
        p.update(params or {})
        _, _, data = self.c.get(path, p)
        return data.get("data") if isinstance(data, dict) else None

    def post(self, path: str, body: dict):
        _, _, data = self.c.post(f"{path}?token={self.c.token}", body)
        return data.get("data") if isinstance(data, dict) else None


def main() -> int:
    nas = Nas()

    machine = nas.get("v1/sysinfo/machine/common") or {}
    common = machine.get("common") or {}
    hw = machine.get("hardware") or {}
    stat = nas.get("v1/taskmgr/stat/overview") or {}
    disks = (nas.get("v2/storage/disk/list") or {}).get("result") or []
    pools = (nas.get("v1/storage/pool/list") or {}).get("result") or []
    volumes = (nas.get("v1/storage/volume/list") or {}).get("result") or []
    ctrs = nas.post("v1/docker/container/ContainerListV2", {"PageNum": 1, "PageSize": 200}) or {}
    ctr_list = ctrs.get("result") or []

    cpu_hw = (hw.get("cpu") or [{}])[0]
    mem_hw = (hw.get("mem") or [{}])[0]
    cpu = (stat.get("cpu") or [{}])[0]
    mem = (stat.get("mem") or [{}])[0]
    net = (stat.get("net") or [{}])[0]
    cfan = (stat.get("cpu_fan") or [{}])[0]
    dfan = (stat.get("device_fan") or [{}])[0]

    line = "=" * 62
    print(line)
    print(f"  {common.get('nas_name')}   {common.get('model')}")
    print(f"  UGOS Pro {common.get('system_version')}   区域 {common.get('product_series')}")
    on_at = common.get("last_turn_on_timestamp")
    on_str = dt.datetime.fromtimestamp(on_at).strftime("%Y-%m-%d %H:%M") if on_at else "-"
    print(f"  已运行 {human_uptime(common.get('run_time') or 0)}   开机于 {on_str}")
    print(line)

    print(f"\nCPU    {cpu_hw.get('model')}  {cpu_hw.get('core')}C{cpu_hw.get('thread')}T  @{cpu_hw.get('ghz')}MHz")
    print(f"       {bar(cpu.get('used_percent') or 0)} {cpu.get('used_percent', 0):.1f}%   {cpu.get('temp', '-')}°C")

    mem_total = mem_hw.get("size") or 0
    mem_used = mem_total * (mem.get("used_percent") or 0) / 100
    print(f"内存   {bar(mem.get('used_percent') or 0)} {mem.get('used_percent', 0):.1f}%   "
          f"{human_bytes(mem_used)} / {human_bytes(mem_total)}   "
          f"{mem_hw.get('manufacturer', '')} DDR5-{str(mem_hw.get('mhz', '')).replace(' MHz', '')}")

    print(f"风扇   机箱 {dfan.get('speed', 0)} RPM  ·  CPU {'停转' if not cfan.get('speed') else str(cfan.get('speed')) + ' RPM'}")

    for nic in hw.get("net") or []:
        print(f"网络   {nic.get('model')} {nic.get('ip')}  链路 {nic.get('speed')}Mbps "
              f"{nic.get('duplex', '')}   ↑{human_rate(net.get('send_rate') or 0)}  ↓{human_rate(net.get('recv_rate') or 0)}")

    print("\n" + "-" * 62)
    for pool in pools:
        used = pool.get("used") or 0
        total = pool.get("total") or 0
        pct = used / total * 100 if total else 0
        print(f"存储池 {pool.get('label')}   {(pool.get('level') or '').upper()}   "
              f"{len(pool.get('disks') or [])} 盘   {human_bytes(total)}")
        if (pool.get("level") or "").lower() in ("raid0", "basic", "jbod"):
            print("       ⚠ 无冗余模式，任一盘损坏即全部数据丢失")

    for vol in volumes:
        used = vol.get("used") or 0
        total = vol.get("total") or 0
        pct = used / total * 100 if total else 0
        print(f"空间   {vol.get('label')}  {vol.get('filesystem')}  "
              f"{bar(pct)} {pct:.0f}%   {human_bytes(used)} / {human_bytes(total)}   {vol.get('mntpath')}")

    print("\n" + "-" * 62)
    print("硬盘")
    for d in disks:
        print(f"  {d.get('label')}  {d.get('brand', '')} {d.get('model')}  "
              f"{human_bytes(d.get('size'))}  {d.get('temperature', '-')}°C  "
              f"通电 {d.get('power_on_hours', 0)}h  SMART {'支持' if d.get('is_support_smart') else '不支持'}  "
              f"{d.get('used_for') or '未使用'}")

    print("\n" + "-" * 62)
    running = [c for c in ctr_list if (c.get("status") or "") == "running"]
    stopped = [c for c in ctr_list if (c.get("status") or "") != "running"]
    print(f"容器   {len(ctr_list)} 个   运行 {len(running)}   非运行 {len(stopped)}")
    for c in stopped:
        print(f"  · {c.get('name'):32s} {c.get('status')}")
    print(f"\n{'=' * 62}\n  数据来源：UGOS 私有接口（token 会话已复用）\n{'=' * 62}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        sys.exit(1)
