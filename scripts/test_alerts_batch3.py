#!/usr/bin/env python3
"""第三批告警规则的状态机验证（合成数据，不碰 NAS）。

为什么用合成数据：容器停止、磁盘唤醒、服务掉线都要求真机进入故障态，
在别人的生产 NAS 上制造故障不可接受。这里用真实 `AlertEngine` + 合成快照，
把阈值、基线、互斥、恢复四个语义逐条跑一遍；数据源是否真实可用由另一组接口实测覆盖。

用法: python scripts/test_alerts_batch3.py
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from app.config import Settings  # noqa: E402
from app.services.alerts import AlertEngine  # noqa: E402
from app.services.power import DiskSleepMonitor  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def snap(containers=None, disks=None, services=None, procs=None, volumes=None, nics=None,
         engine="online") -> dict:
    system = {"nics": nics or [], "ups": None}
    return {
        "system": system,
        "live": {"cpu": {}, "memory": {}, "fans": {}},
        "storage": {"disks": disks or [], "pools": [], "volumes": volumes or []},
        "containers": containers or [],
        "smart": [],
        "service_health": {
            "ugos_services": {"items": [{"id": k, "label": k} for k in (services or [])]},
            "system_processes": {
                "watched": [
                    {"name": k, "label": k, "present": v}
                    for k, v in (procs or {}).items()
                ]
            },
            "docker_engine": {"status": engine, "healthy": engine == "online"},
            "required": {"configured": []},
        },
    }


def keys(events) -> dict[str, str]:
    return {ev.alert.key: ev.action for ev in events}


async def case_container_expected(s: Settings) -> None:
    print("\n[1] 容器「期望运行」基线 → 停止告警 → 恢复")
    eng = AlertEngine(s)
    c = [{"name": "qb", "running": True, "status": "running", "image": "x"}]
    for _ in range(3):
        await eng.evaluate(snap(containers=c))
    base = eng.container_baseline()
    check("连续 3 轮 running 建立基线", base["total"] == 1, str(base["items"]))

    ev = await eng.evaluate(snap(containers=[{"name": "qb", "running": False,
                                              "status": "exited", "image": "x"}]))
    k = keys(ev)
    check("首次观察到停止不立即告警（需 2 轮确认）",
          "container_expected_down:qb" not in k, str(k))
    ev = await eng.evaluate(snap(containers=[{"name": "qb", "running": False,
                                              "status": "exited", "image": "x"}]))
    k = keys(ev)
    check("连续 2 轮停止 → container_expected_down", k.get("container_expected_down:qb") == "raised", str(k))
    check("不再重复发 container_down", "container_down:qb" not in k, str(k))

    ev = await eng.evaluate(snap(containers=c))
    check("恢复运行 → resolved", keys(ev).get("container_expected_down:qb") == "resolved",
          str(keys(ev)))


async def case_container_restart_exclusive(s: Settings) -> None:
    print("\n[2] 反复重启与「应运行但停止」互斥")
    eng = AlertEngine(s)
    c = [{"name": "web", "running": True, "status": "running", "image": "y"}]
    for _ in range(3):
        await eng.evaluate(snap(containers=c))
    raised: set[str] = set()
    # 停 → 起 反复 4 次，超过 restart 阈值 3
    for i in range(4):
        ev = await eng.evaluate(snap(containers=[{"name": "web", "running": False,
                                                  "status": "exited", "image": "y"}]))
        raised |= set(keys(ev))
        ev = await eng.evaluate(snap(containers=c))
        raised |= set(keys(ev))
    check("命中 container_restart_loop", "container_restart_loop:web" in raised, str(raised))
    check("未叠加 container_expected_down", "container_expected_down:web" not in raised, str(raised))
    check("未叠加 container_down", "container_down:web" not in raised, str(raised))


async def case_container_down_unbaselined(s: Settings) -> None:
    print("\n[3] 未建立基线的容器 → 仍走原 container_down")
    eng = AlertEngine(s)
    c = [{"name": "new", "running": True, "status": "running", "image": "z"}]
    await eng.evaluate(snap(containers=c))          # 第 1 轮：首轮只建基线
    stopped = [{"name": "new", "running": False, "status": "exited", "image": "z"}]
    await eng.evaluate(snap(containers=stopped))    # 第 2 轮：计数 1
    ev = await eng.evaluate(snap(containers=stopped))  # 第 3 轮：计数 2
    k = keys(ev)
    check("container_down raised", k.get("container_down:new") == "raised", str(k))
    check("未误发 expected_down", "container_expected_down:new" not in k, str(k))


async def case_suppress(s: Settings) -> None:
    print("\n[4] 用户主动停止（suppress）后不再告警")
    eng = AlertEngine(s)
    c = [{"name": "app", "running": True, "status": "running", "image": "w"}]
    for _ in range(3):
        await eng.evaluate(snap(containers=c))
    eng.suppress("app", True)
    ev = await eng.evaluate(snap(containers=[{"name": "app", "running": False,
                                              "status": "exited", "image": "w"}]))
    check("被 suppress 的容器不产生事件", keys(ev) == {}, str(keys(ev)))


async def case_disk_wake(s: Settings) -> None:
    print("\n[5] 磁盘频繁唤醒：窗口内 6 次 → 告警，停止唤醒 → 恢复")
    mon = DiskSleepMonitor(s)
    eng = AlertEngine(s, mon)
    sleeping = [{"name": "sda", "label": "硬盘1", "standby": True}]
    active = [{"name": "sda", "label": "硬盘1", "standby": False}]

    await eng.evaluate(snap(disks=sleeping))         # 建基线
    raised = False
    for i in range(s.alert_disk_wake_count):
        ev = await eng.evaluate(snap(disks=active))   # 醒
        raised |= keys(ev).get("disk_wake_freq:sda") == "raised"
        await eng.evaluate(snap(disks=sleeping))      # 又睡
    check(f"唤醒 {s.alert_disk_wake_count} 次后告警", raised, str(mon.summary()))
    check("唤醒计数正确", mon.wakes("sda") == s.alert_disk_wake_count,
          f"wakes={mon.wakes('sda')}")

    # 让窗口内唤醒次数回落：直接清空统计（等价于时间窗口滑过）
    mon._wakes["sda"].clear()  # noqa: SLF001 - 测试脚本直接改内部窗口
    ev = await eng.evaluate(snap(disks=active))
    check("窗口滑过 → resolved", keys(ev).get("disk_wake_freq:sda") == "resolved",
          str(keys(ev)))


async def case_service(s: Settings) -> None:
    print("\n[6] 关键服务：基线型 + 强制型 + Docker 引擎")
    eng = AlertEngine(s)
    healthy = {"smbd": True, "nfsd": False, "dockerd": True}
    for _ in range(s.alert_service_expected_polls):
        await eng.evaluate(snap(procs=healthy, engine="online"))

    down = {"smbd": False, "nfsd": False, "dockerd": True}
    ev = await eng.evaluate(snap(procs=down, engine="online"))
    k = keys(ev)
    check("smbd 消失 → service_down", k.get("service_down:smbd") == "raised", str(k))
    check("从未出现的 nfsd 不误报", "service_down:nfsd" not in k, str(k))

    ev = await eng.evaluate(snap(procs=healthy, engine="online"))
    check("smbd 恢复 → resolved", keys(ev).get("service_down:smbd") == "resolved",
          str(keys(ev)))

    ev = await eng.evaluate(snap(procs=healthy, engine="stopped"))
    check("Docker 引擎异常 → critical",
          keys(ev).get("service_down:docker-engine") == "raised", str(keys(ev)))

    s2 = s.model_copy(update={"alert_required_services": "nfsd"})
    eng2 = AlertEngine(s2)
    ev = await eng2.evaluate(snap(procs=healthy, engine="online"))
    check("ALERT_REQUIRED_SERVICES 点名缺失 → 首轮即告警",
          keys(ev).get("service_down:nfsd") == "raised", str(keys(ev)))


async def case_inode_and_nic(s: Settings) -> None:
    print("\n[7] inode / 网口错误包（有数据才触发）")
    eng = AlertEngine(s)
    vol = [{"name": "volume1", "used_percent": 10, "inode_used_percent": 92, "mount": "/v1"}]
    ev = await eng.evaluate(snap(volumes=vol))
    check("inode 92% → volume_inode", keys(ev).get("volume_inode:volume1") == "raised",
          str(keys(ev)))

    eng2 = AlertEngine(s)
    nic = [{"name": "eth0", "speed_mbps": 1000, "duplex": "full",
            "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}]
    await eng2.evaluate(snap(nics=nic))
    nic2 = [{"name": "eth0", "speed_mbps": 1000, "duplex": "full",
             "rx_errors": s.alert_nic_error_delta + 5, "tx_errors": 0,
             "rx_dropped": 0, "tx_dropped": 0}]
    ev = await eng2.evaluate(snap(nics=nic2))
    check("错误包增量超阈 → nic_errors", keys(ev).get("nic_errors:eth0") == "raised",
          str(keys(ev)))

    eng3 = AlertEngine(s)
    ev = await eng3.evaluate(snap(volumes=[{"name": "v", "inode_used_percent": None}]))
    check("无 inode 数据时不误报", "volume_inode:v" not in keys(ev), str(keys(ev)))


async def main() -> int:
    s = Settings(
        alert_disk_wake_count=6,
        alert_disk_wake_window=3600,
        alert_container_expected_polls=3,
        alert_container_restart_count=3,
        alert_container_restart_window=600,
        alert_service_expected_polls=3,
        alert_nic_error_delta=100,
        alert_volume_inode=90,
    )
    await case_container_expected(s)
    await case_container_restart_exclusive(s)
    await case_container_down_unbaselined(s)
    await case_suppress(s)
    await case_disk_wake(s)
    await case_service(s)
    await case_inode_and_nic(s)

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{'=' * 50}\n{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        for _, name, detail in failed:
            print(f"  FAIL {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
