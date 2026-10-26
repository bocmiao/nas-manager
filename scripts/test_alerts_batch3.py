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

    # 用户主动停止时，已存在的告警必须收尾（否则 App 永远收不到「已恢复」）
    eng2 = AlertEngine(s)
    for _ in range(3):
        await eng2.evaluate(snap(containers=c))
    stopped = [{"name": "app", "running": False, "status": "exited", "image": "w"}]
    await eng2.evaluate(snap(containers=stopped))
    ev = await eng2.evaluate(snap(containers=stopped))
    check("先制造 container_expected_down",
          keys(ev).get("container_expected_down:app") == "raised", str(keys(ev)))
    events = eng2.suppress("app", True)
    check("suppress 撤回告警并给出 resolved 事件",
          keys(events).get("container_expected_down:app") == "resolved", str(keys(events)))
    check("告警已从 active 移除",
          not any(a.key.startswith(("container_down:app", "container_expected_down:app"))
                  for a in eng2.active()))


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


async def case_degraded_no_reset(s: Settings) -> None:
    """数据源降级时只能「不新增不恢复」，绝不能把基线当成故障消失。

    这是最容易失守的一条：降级时用空列表去评估相对量规则，清理循环会把基线全删，
    于是「容器挂了」在抖动一次之后就永久不再报警。
    """
    print("\n[8] 数据源降级不清基线（容器 / 网口 / SMART / 服务）")
    eng = AlertEngine(s)
    c = [{"name": "qb", "running": True, "status": "running", "image": "x"}]
    stopped = [{"name": "qb", "running": False, "status": "exited", "image": "x"}]
    for _ in range(3):
        await eng.evaluate(snap(containers=c))
    await eng.evaluate(snap(containers=stopped))
    ev = await eng.evaluate(snap(containers=stopped))
    check("先制造 container_expected_down",
          keys(ev).get("container_expected_down:qb") == "raised", str(keys(ev)))

    ev = await eng.evaluate({**snap(containers=[]), "degraded": ["containers"]})
    check("容器降级时不发 resolved（取不到≠已恢复）", keys(ev) == {}, str(keys(ev)))
    check("容器降级后「期望运行」基线仍在",
          any(i["name"] == "qb" for i in eng.container_baseline()["items"]),
          str(eng.container_baseline()["items"]))
    check("容器降级后告警仍在 active",
          any(a.key == "container_expected_down:qb" for a in eng.active()))

    ev = await eng.evaluate(snap(containers=stopped))
    check("数据恢复后不重复 raise", keys(ev) == {}, str(keys(ev)))
    ev = await eng.evaluate(snap(containers=c))
    check("真正恢复运行 → resolved",
          keys(ev).get("container_expected_down:qb") == "resolved", str(keys(ev)))

    # 网口：降级时不能把 active 的 nic_errors 判成恢复
    eng2 = AlertEngine(s)
    n0 = [{"name": "eth0", "speed_mbps": 2500, "rx_errors": 0}]
    await eng2.evaluate(snap(nics=n0))
    n1 = [{"name": "eth0", "speed_mbps": 2500, "rx_errors": s.alert_nic_error_delta + 1}]
    ev = await eng2.evaluate(snap(nics=n1))
    check("先制造 nic_errors", keys(ev).get("nic_errors:eth0") == "raised", str(keys(ev)))
    ev = await eng2.evaluate({**snap(nics=[]), "degraded": ["system"]})
    check("网口降级时不判恢复", keys(ev) == {}, str(keys(ev)))
    ev = await eng2.evaluate(snap(nics=[{"name": "eth0", "speed_mbps": 1000, "duplex": "full"}]))
    check("降级后仍按历史峰值 2500 Mbps 判降速",
          keys(ev).get("nic_speed:eth0") == "raised", str(keys(ev)))

    # SMART：休眠盘没有数据，不能当成「已恢复正常」
    eng3 = AlertEngine(s)
    bad = snap()
    bad["smart"] = [{"supported": True, "disk": {"name": "sda", "label": "硬盘1"},
                     "counters": {"reallocated_sectors": 8},
                     "key_items": {"power_on_hours": 1200}, "health": "ok"}]
    ev = await eng3.evaluate(bad)
    check("有重映射扇区 → disk_smart raised",
          keys(ev).get("disk_smart:sda") == "raised", str(keys(ev)))
    skipped = snap()
    skipped["smart"] = []
    skipped["smart_skipped"] = ["sda"]
    ev = await eng3.evaluate(skipped)
    check("休眠盘本轮无 SMART 数据 → 不判恢复", keys(ev) == {}, str(keys(ev)))
    check("SMART 告警仍在 active",
          any(a.key == "disk_smart:sda" for a in eng3.active()))
    ev = await eng3.evaluate(bad)
    check("唤醒后不重复 raise", keys(ev) == {}, str(keys(ev)))

    # 服务：进程表空 → 不可信，不能一次性把十几个守护进程判成未运行
    eng4 = AlertEngine(s)
    for _ in range(s.alert_service_expected_polls):
        await eng4.evaluate(snap(procs={"smbd": True}))
    ev = await eng4.evaluate(snap(procs={"smbd": False}))
    check("先制造 service_down", keys(ev).get("service_down:smbd") == "raised", str(keys(ev)))
    empty = snap(procs={})
    empty["service_health"]["system_processes"]["reliable"] = False
    ev = await eng4.evaluate(empty)
    check("进程表为空 → 不新增也不撤销服务告警",
          keys(ev) == {} and any(a.key == "service_down:smbd" for a in eng4.active()),
          str(keys(ev)))
    ev = await eng4.evaluate(snap(procs={"smbd": True}))
    check("进程表恢复且服务在跑 → resolved",
          keys(ev).get("service_down:smbd") == "resolved", str(keys(ev)))


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
    await case_degraded_no_reset(s)

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{'=' * 50}\n{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        for _, name, detail in failed:
            print(f"  FAIL {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
