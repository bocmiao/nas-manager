#!/usr/bin/env python3
"""降级场景回归：数据源挂掉时不能把「取不到数据」当成「故障消失」。

用真实的 `gather_snapshot` + 真实 `AlertEngine`，数据源换成会按需抛异常的假对象，
不碰 NAS。覆盖两处高危缺陷：

1. `containers` 降级时若仍用空列表评估容器规则，清理循环会把「期望运行」基线全删，
   结果 NAS 抖一次之后已停止的容器永久不再报警，active 告警还会被误报成恢复。
2. `/health` 的 `ugos.reachable` 只看「巡检有没有抛异常」，而 `gather_snapshot`
   用 `return_exceptions=True` 吞掉了所有数据源异常，于是 NAS 全挂也回 true。

用法: python scripts/test_degraded_snapshot.py
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from app.config import Settings  # noqa: E402
from app.main import judge_reachable  # noqa: E402
from app.services.alerts import AlertEngine  # noqa: E402
from app.services.snapshot import gather_snapshot  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))


class FakeNas:
    """按 `fail` 集合模拟某个数据源超时。"""

    def __init__(self) -> None:
        self.fail: set[str] = set()
        self.container_list = [
            {"name": "qb", "id": "0" * 12, "status": "running", "running": True,
             "image": "demo:latest"}
        ]

    async def _maybe(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"{name} 数据源不可用（模拟超时）")

    async def system(self):
        await self._maybe("system")
        return {"nics": [], "ups": None}

    async def live(self):
        await self._maybe("live")
        return {"cpu": {}, "memory": {}, "fans": {}}

    async def storage(self):
        await self._maybe("storage")
        return {"disks": [], "pools": [], "volumes": []}

    async def containers(self):
        await self._maybe("containers")
        return self.container_list

    async def all_smart(self):
        await self._maybe("smart")
        return [], []


class FakeHealth:
    async def services(self):
        return {
            "ugos_services": {"items": []},
            "system_processes": {"watched": [], "total": 300, "reliable": True},
            "docker_engine": {"status": "online", "healthy": True},
        }


def keys(events) -> dict[str, str]:
    return {ev.alert.key: ev.action for ev in events}


async def main() -> int:
    s = Settings(alert_container_expected_polls=3)
    nas = FakeNas()
    eng = AlertEngine(s)

    print("\n[1] 建基线（3 轮 running）")
    for _ in range(3):
        snap = await gather_snapshot(nas, FakeHealth())
        await eng.evaluate(snap)
        check("快照无降级", not snap.get("degraded"), str(snap.get("degraded")))
        check("reachable=True", judge_reachable(snap.get("degraded")))
    check("建立「期望运行」基线", eng.container_baseline()["total"] == 1)

    print("\n[2] 容器停止 → 2 轮确认 → 告警")
    nas.container_list[0].update({"running": False, "status": "exited"})
    ev = []
    for _ in range(2):
        ev = await eng.evaluate(await gather_snapshot(nas, FakeHealth()))
    check("container_expected_down raised",
          keys(ev).get("container_expected_down:qb") == "raised", str(keys(ev)))

    print("\n[3] NAS 抖动：containers 数据源失败一轮")
    nas.fail = {"containers"}
    snap = await gather_snapshot(nas, FakeHealth())
    check("快照标记 degraded=containers",
          snap.get("degraded") == ["containers"], str(snap.get("degraded")))
    check("containers 字段为空数组", snap.get("containers") == [])
    ev = await eng.evaluate(snap)
    check("降级轮不产生任何事件（不误报已恢复）", keys(ev) == {}, str(keys(ev)))
    check("告警仍在 active", any(a.key == "container_expected_down:qb" for a in eng.active()))
    check("基线未被清空", eng.container_baseline()["total"] == 1,
          str(eng.container_baseline()["items"]))
    check("单源降级仍算 reachable", judge_reachable(snap.get("degraded")))

    print("\n[4] 五个核心源全挂")
    nas.fail = {"system", "live", "storage", "containers", "smart"}
    snap = await gather_snapshot(nas, FakeHealth())
    check("降级清单含全部核心源",
          {"system", "live", "storage", "containers", "smart"} <= set(snap.get("degraded") or []),
          str(snap.get("degraded")))
    check("全降级 → reachable=False", judge_reachable(snap.get("degraded")) is False)
    ev = await eng.evaluate(snap)
    check("全降级轮仍不误报恢复", keys(ev) == {}, str(keys(ev)))
    check("告警仍在 active", any(a.key == "container_expected_down:qb" for a in eng.active()))
    check("SMART 降级时跳过集合为空且不崩",
          snap.get("smart") == [] and snap.get("smart_skipped") == [])

    print("\n[5] 只有 system+live 挂")
    nas.fail = {"system", "live"}
    snap = await gather_snapshot(nas, FakeHealth())
    check("system+live 同时降级 → reachable=False",
          judge_reachable(snap.get("degraded")) is False)

    print("\n[6] 数据源恢复：基线保留，无需重建")
    nas.fail = set()
    ev = await eng.evaluate(await gather_snapshot(nas, FakeHealth()))
    check("恢复后不重复 raise", keys(ev) == {}, str(keys(ev)))
    check("reachable 复位为 True", judge_reachable([]) is True)
    nas.container_list[0].update({"running": True, "status": "running"})
    ev = await eng.evaluate(await gather_snapshot(nas, FakeHealth()))
    check("容器真的跑起来才算恢复",
          keys(ev).get("container_expected_down:qb") == "resolved", str(keys(ev)))

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{'=' * 50}\n{len(results) - len(failed)}/{len(results)} 通过")
    for _, name, detail in failed:
        print(f"  FAIL {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
