#!/usr/bin/env python3
"""第四批新增规则与解析函数验证（合成数据，不碰 NAS）。

为什么要合成数据：
- 登录爆破告警如果要在真机触发，就得真的把账号打到锁定（NAS 自带 faillock 5 分钟 6 次），
  这会直接影响别人的生产设备，不能做。真机上只验证了「一次受控失败登录」的日志形态。
- 备份任务告警需要 NAS 上存在失败/超期的备份任务，本机一个任务都没配，不能为了测试去建。

所以这里用真实 `AlertEngine` + 合成快照，把阈值、升级、恢复、降级四个语义跑一遍；
另外把几个纯函数（日志级别别名、登录日志解析、备份任务归一化、VM 应用识别、电源星期映射）
的边界也跑一遍。数据源本身是否可用由 `probe_batch4.py` + 接口实测覆盖。

用法: python scripts/test_alerts_batch4.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import asyncio  # noqa: E402

from app.config import Settings  # noqa: E402
from app.services.alerts import AlertEngine, _parse_time  # noqa: E402
from app.services.backup import normalize_task  # noqa: E402
from app.services.logs import normalize_level  # noqa: E402
from app.services.powerplan import days_label, normalize_switch  # noqa: E402
from app.services.sessions import parse_login_record  # noqa: E402
from app.services.vms import find_vm_modules, normalize_vm  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def snap(login=None, backup=None, degraded=None) -> dict:
    """最小快照：只保留第四批规则关心的字段 + 其它规则需要的空壳。"""
    s = {
        "system": {"nics": [], "ups": None},
        "live": {"cpu": {}, "memory": {}, "fans": {}},
        "storage": {"disks": [], "pools": [], "volumes": []},
        "containers": [],
        "smart": [],
    }
    if login is not None:
        s["login_failures"] = login
    if backup is not None:
        s["backup"] = backup
    if degraded:
        s["degraded"] = degraded
    return s


def failures(items) -> dict:
    return {
        "window_seconds": 300,
        "scanned": sum(i["count"] for i in items),
        "failed_total": sum(i["count"] for i in items),
        "items": items,
        "threshold": 10,
    }


def task(tid=1, name="夜间备份", status=0, last=None, schedule=None) -> dict:
    return {
        "id": tid,
        "name": name,
        "status_code": status,
        "status_label": {0: "已完成", 1: "失败", 5: "未开始", 6: "等待中"}.get(status, "未知"),
        "failed": status == 1,
        "running": status in (2, 4, 6),
        "progress": 100,
        "protocol": "local",
        "last_run_at": last,
        "next_run_at": None,
        "schedule_time": schedule,
    }


def keys(events) -> dict[str, str]:
    return {ev.alert.key: ev.action for ev in events}


# ------------------------------------------------------------------ 登录爆破

async def case_login_bruteforce(s: Settings) -> None:
    print("\n[1] 同一 IP 登录失败达阈值 → 告警 → 升级 → 恢复")
    eng = AlertEngine(s)
    ip = "192.0.2.10"

    ev = await eng.evaluate(snap(login=failures([{"ip": ip, "count": 9, "users": ["u1"]}])))
    check("9 次（未达阈值 10）不告警", "login_bruteforce:" + ip not in keys(ev), str(keys(ev)))

    ev = await eng.evaluate(snap(login=failures([{"ip": ip, "count": 10, "users": ["u1"],
                                                  "last_at": int(time.time())}])))
    k = keys(ev)
    check("10 次 → login_bruteforce raised", k.get(f"login_bruteforce:{ip}") == "raised", str(k))
    active = {a.key: a for a in eng.active()}
    check("级别 warning", active.get(f"login_bruteforce:{ip}").level == "warning")
    check("detail 里有次数与账号",
          "10 次" in active[f"login_bruteforce:{ip}"].detail)

    ev = await eng.evaluate(snap(login=failures([{"ip": ip, "count": 10, "users": ["u1"]}])))
    check("仍在阈值上不重复发事件", keys(ev) == {}, str(keys(ev)))

    ev = await eng.evaluate(snap(login=failures([{"ip": ip, "count": 20, "users": ["u1", "root"]}])))
    check("20 次（=2 倍阈值）升级为 critical",
          {a.key: a.level for a in eng.active()}.get(f"login_bruteforce:{ip}") == "critical")
    check("升级不产生新事件（只在 raised/resolved 跳变时发）", keys(ev) == {}, str(keys(ev)))

    ev = await eng.evaluate(snap(login=failures([])))
    check("窗口内没有失败 → resolved", keys(ev).get(f"login_bruteforce:{ip}") == "resolved",
          str(keys(ev)))


async def case_login_degraded(s: Settings) -> None:
    print("\n[2] 数据源降级时不误判恢复")
    eng = AlertEngine(s)
    ip = "192.0.2.11"
    await eng.evaluate(snap(login=failures([{"ip": ip, "count": 12, "users": ["u1"]}])))
    check("先制造一条告警", any(a.key == f"login_bruteforce:{ip}" for a in eng.active()))

    ev = await eng.evaluate(snap(login=failures([]), degraded=["login_failures"]))
    check("degraded 时不产生 resolved（数据取不到≠故障消失）",
          keys(ev) == {}, str(keys(ev)))
    check("告警仍在 active 列表", any(a.key == f"login_bruteforce:{ip}" for a in eng.active()))

    ev = await eng.evaluate(snap(login=failures([])))
    check("数据恢复且确实无失败 → resolved",
          keys(ev).get(f"login_bruteforce:{ip}") == "resolved", str(keys(ev)))


# ------------------------------------------------------------------ 备份任务

async def case_backup_failed(s: Settings) -> None:
    print("\n[3] 备份任务失败 → 告警 → 恢复")
    eng = AlertEngine(s)
    ev = await eng.evaluate(snap(backup={"items": [task(status=0)]}))
    check("任务正常不告警", keys(ev) == {}, str(keys(ev)))

    ev = await eng.evaluate(snap(backup={"items": [task(status=1)]}))
    k = keys(ev)
    check("状态=失败 → backup_task_failed raised",
          k.get("backup_task_failed:1") == "raised", str(k))
    active = {a.key: a for a in eng.active()}
    check("告警标题含任务名", "夜间备份" in active["backup_task_failed:1"].title)

    ev = await eng.evaluate(snap(backup={"items": [task(status=0)]}))
    check("恢复正常 → resolved", keys(ev).get("backup_task_failed:1") == "resolved",
          str(keys(ev)))


async def case_backup_stale(s: Settings) -> None:
    print("\n[4] 备份任务超期未运行（阈值 48 小时）")
    eng = AlertEngine(s)
    now = time.time()

    ev = await eng.evaluate(snap(backup={"items": [
        {"id": 7, "name": "周备份", "status_code": 0, "failed": False,
         "last_run_at": int(now - 3600 * 3)}
    ]}))
    check("3 小时前跑过 → 不告警", keys(ev) == {}, str(keys(ev)))

    ev = await eng.evaluate(snap(backup={"items": [
        {"id": 7, "name": "周备份", "status_code": 0, "failed": False,
         "last_run_at": int(now - 3600 * 72)}
    ]}))
    k = keys(ev)
    check("72 小时前跑过 → backup_task_stale raised", k.get("backup_task_stale:7") == "raised",
          str(k))
    active = {a.key: a for a in eng.active()}
    check("value 是超期小时数（≈72）", abs((active["backup_task_stale:7"].value or 0) - 72) < 1,
          str(active["backup_task_stale:7"].value))

    # 没有上次运行时间，但计划时间已过且状态仍是未开始
    past = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 3600 * 60))
    ev = await eng.evaluate(snap(backup={"items": [
        {"id": 8, "name": "任务B", "status_code": 5, "status_label": "未开始",
         "failed": False, "last_run_at": None, "schedule_time": past}
    ]}))
    k = keys(ev)
    check("无 last_run 但计划时间已过 60h → 用计划时间判定超期",
          k.get("backup_task_stale:8") == "raised", str(k))
    # 同一任务既失败又超期：只发失败，不叠加超期
    ev = await eng.evaluate(snap(backup={"items": [
        {"id": 10, "name": "任务D", "status_code": 1, "status_label": "失败",
         "failed": True, "last_run_at": int(now - 3600 * 100)}
    ]}))
    k = keys(ev)
    check("既失败又超期时只发 backup_task_failed",
          k.get("backup_task_failed:10") == "raised" and "backup_task_stale:10" not in k, str(k))

    # 两条证据都没有 → 静默
    ev = await eng.evaluate(snap(backup={"items": [
        {"id": 9, "name": "任务C", "status_code": 0, "failed": False,
         "last_run_at": None, "schedule_time": None}
    ]}))
    check("既没上次运行时间也没计划时间 → 静默跳过（不自造数据）",
          "backup_task_stale:9" not in keys(ev), str(keys(ev)))


async def case_backup_degraded(s: Settings) -> None:
    print("\n[5] 备份数据源降级")
    eng = AlertEngine(s)
    await eng.evaluate(snap(backup={"items": [task(status=1)]}))
    ev = await eng.evaluate(snap(backup={}, degraded=["backup"]))
    check("degraded 时不 resolved", keys(ev) == {}, str(keys(ev)))
    check("告警仍在", any(a.key == "backup_task_failed:1" for a in eng.active()))


# ------------------------------------------------------------------ 纯函数

def case_parsers(s: Settings) -> None:
    print("\n[6] 日志级别别名")
    check("error → critical", normalize_level("error") == "critical")
    check("warn → warning", normalize_level("warn") == "warning")
    check("INFO → info（大小写不敏感）", normalize_level("INFO") == "info")
    try:
        normalize_level("debug")
        ok = False
    except ValueError:
        ok = True
    check("debug 被拒（NAS 只认三档）", ok)

    print("\n[7] 登录审计正文解析")
    r1 = parse_login_record({"content": "User [u1] from [192.0.2.10] logged in successfully.",
                             "level": "info"})
    check("成功记录", r1["result"] == "success" and r1["ip"] == "192.0.2.10"
          and r1["username"] == "u1" and r1["method"] == "web", str(r1["result"]))
    r2 = parse_login_record({
        "content": "User [u1] from [192.0.2.10] failed to log in due to incorrect username or password.",
        "level": "warning"})
    check("失败记录", r2["result"] == "failed" and r2["ip"] == "192.0.2.10", str(r2["result"]))
    r3 = parse_login_record({"content": "User u1 logged into the system via SSH.", "level": "info"})
    check("SSH 记录（用户名在正文、无 IP）",
          r3["result"] == "success" and r3["method"] == "ssh" and r3["username"] == "u1",
          f"{r3['result']}/{r3['method']}")
    r4 = parse_login_record({"content": "something unknown happened", "level": "info"})
    check("未知文案不猜成功", r4["result"] == "unknown", r4["result"])

    print("\n[8] 备份任务归一化")
    t = normalize_task({"id": 3, "task_name": "相册备份", "status": 4, "progress": 0.55,
                        "backup_type": 1, "sync_mode": "increment", "schedule_type": 3,
                        "backup_local_paths": ["/volume1/a"], "backup_remote_paths": ["/volume1/b"]})
    check("状态 4 → progress / running", t["status"] == "progress" and t["running"] is True,
          t["status"])
    check("0.55 → 55%", t["progress"] == 55.0, str(t["progress"]))
    check("备份类型 1 → 增量", t["backup_type"]["key"] == "incremental")
    check("计划类型 3 → 计划", t["schedule_type"]["key"] == "plan")
    check("路径字段", t["local_paths"] == ["/volume1/a"], str(t["local_paths"]))
    t2 = normalize_task({"id": 4, "status": "error"})
    check("字符串状态 error → 失败", t2["failed"] is True and t2["status_code"] == 1, t2["status"])

    print("\n[9] 电源计划星期映射")
    check("1-5 → 工作日", days_label([1, 2, 3, 4, 5]) == "工作日")
    check("6,7 → 周末", days_label([6, 7]) == "周末")
    check("1-7 → 每天", days_label([7, 1, 2, 3, 4, 5, 6]) == "每天")
    check("单日 → 周一", days_label([1]) == "周一")
    sw = normalize_switch({"frequency": [7, 1, 2, 3, 4, 5, 6], "time": "08:00", "action": "open"})
    check("开关归一化", sw["days_label"] == "每天" and sw["action_label"] == "开机", str(sw))
    check("_parse_time 解析 'YYYY-MM-DD HH:MM:SS'",
          _parse_time("2026-10-26 08:00:00") is not None)
    check("_parse_time 解析 Unix 秒", _parse_time(1792976494) == 1792976494.0)
    check("_parse_time 无法解析返回 None", _parse_time("明天早上") is None)

    print("\n[10] 虚拟机应用识别")
    mods = [
        {"module_id": "com.ugreen.docker", "name": "Docker"},
        {"module_id": "com.ugreen.virtualmachine", "name": "虚拟机", "entry": "/vm"},
        {"module_id": "com.ugreen.versionmgr", "name": "文件版本管理"},
    ]
    hits = find_vm_modules(mods)
    check("命中虚拟机的应用", len(hits) == 1 and hits[0]["module_id"] == "com.ugreen.virtualmachine",
          str(hits))
    check("versionmgr 不被误判", all("versionmgr" not in str(h["module_id"]) for h in hits))
    check("当前机器（无虚拟机应用）识别为空", find_vm_modules(
        [{"module_id": "com.ugreen.docker", "name": "Docker"}]) == [])
    vm = normalize_vm({"vm_id": 1, "vm_name": "win11", "state": "running", "vcpu": 4})
    check("VM 条目归一化", vm["name"] == "win11" and vm["running"] is True and vm["cpu_cores"] == 4,
          str({k: vm[k] for k in ("name", "running", "cpu_cores")}))


async def main() -> int:
    s = Settings()
    print(f"阈值：登录失败 {s.alert_login_fail_count} 次 / {s.alert_login_fail_window}s，"
          f"备份超期 {s.alert_backup_stale_hours:g} 小时")
    await case_login_bruteforce(s)
    await case_login_degraded(s)
    await case_backup_failed(s)
    await case_backup_stale(s)
    await case_backup_degraded(s)
    case_parsers(s)

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for st, name, detail in failed:
        print(f"  {st} {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
