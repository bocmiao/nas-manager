"""磁盘测速的异步任务封装。

UGOS 的测速接口本身是「发起 + 轮询」，前端每 2 秒查一次 benchmark/running。
这里把它包成有状态的本地任务：发起后立刻返回任务号，后台协程轮询进度，
上层随时查任务状态，不阻塞请求。

实测契约：
- `POST v1/storage/disk/benchmark/start` body `{"disk": "/dev/sdX"}`
  必须带 `X-CurPass-Token`（RSA 加密密码），否则 code 1101「密码验证失败」
- 磁盘若已在存储池中使用，返回 code 9999 `debug="disk is busy: 1"`
- `GET v1/storage/disk/benchmark/running?disk=/dev/sdX` 测速中返回
  `{start_at, read_perf, write_perf}`，空闲返回 null
- `GET v1/storage/disk/benchmark/info?disk=/dev/sdX` 返回历史 `detection_list`
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..ugos.client import UgosError
from .nas import NasService

log = logging.getLogger("benchmark")


@dataclass
class BenchmarkJob:
    disk: str            # dev 名，形如 /dev/sda
    state: str = "starting"   # starting / running / finished / stopped / failed
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    samples: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "disk": self.disk,
            "state": self.state,
            "started_at": int(self.started_at),
            "finished_at": int(self.finished_at) if self.finished_at else None,
            "elapsed_seconds": int((self.finished_at or time.time()) - self.started_at),
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "sample_count": len(self.samples),
            "samples": self.samples[-12:],
        }


class BenchmarkManager:
    """同一块盘同时只允许一个测速任务。"""

    def __init__(self, nas: NasService, settings: Settings) -> None:
        self._nas = nas
        self._s = settings
        self._jobs: dict[str, BenchmarkJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 查询

    def status(self, disk: str) -> dict | None:
        job = self._jobs.get(disk)
        return job.to_dict() if job else None

    def all_jobs(self) -> list[dict]:
        return [j.to_dict() for j in self._jobs.values()]

    def busy(self) -> bool:
        return any(j.state in ("starting", "running") for j in self._jobs.values())

    # ---------------------------------------------------------------- 控制

    async def start(self, ref: str) -> dict:
        from ..ugos.client import UgosError as _E  # 局部引用避免循环导入告警

        disk = await self._nas.resolve_disk(ref)
        dev = f"/dev/{disk.get('name')}"

        if not self._s.benchmark_allow_start:
            raise PermissionError("服务端已禁用磁盘测速（BENCHMARK_ALLOW_START=false）")

        async with self._lock:
            job = self._jobs.get(dev)
            if job and job.state in ("starting", "running"):
                return job.to_dict()

            # 先确认 NAS 侧没有残留任务，避免本地状态与真机不一致
            try:
                running = await self._nas._c.get(  # noqa: SLF001 - 同层模块，直接复用会话
                    "v1/storage/disk/benchmark/running", {"disk": dev}
                )
            except _E:
                running = None

            try:
                await self._nas._c.post_privileged(  # noqa: SLF001
                    "v1/storage/disk/benchmark/start", {"disk": dev}
                )
            except _E as exc:
                # 9999 + debug="disk is busy: 1" 说明盘在用，属正常业务拒绝
                hint = ""
                if "busy" in (exc.debug or "").lower():
                    hint = "磁盘正在被存储池使用，UGOS 不允许对在用磁盘测速"
                detail = exc.msg + (f"（{exc.debug}）" if exc.debug else "")
                raise BenchmarkRejected(detail, hint=hint) from exc

            job = BenchmarkJob(disk=dev, state="running")
            if running:
                job.progress = running
            self._jobs[dev] = job
            self._tasks[dev] = asyncio.create_task(self._poll(job))
            log.info("磁盘测速已启动 %s", dev)
            return job.to_dict()

    async def stop(self, ref: str) -> dict:
        disk = await self._nas.resolve_disk(ref)
        dev = f"/dev/{disk.get('name')}"
        job = self._jobs.get(dev)
        if not job or job.state not in ("starting", "running"):
            raise ValueError(f"{dev} 当前没有进行中的测速任务")

        try:
            await self._nas._c.post_privileged(  # noqa: SLF001
                "v1/storage/disk/benchmark/stop", {"disk": dev}
            )
        except UgosError as exc:
            job.state = "failed"
            job.error = str(exc)
            job.finished_at = time.time()
            raise

        job.state = "stopped"
        job.finished_at = time.time()
        task = self._tasks.pop(dev, None)
        if task:
            task.cancel()
        return job.to_dict()

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

    # ---------------------------------------------------------------- 轮询

    async def _poll(self, job: BenchmarkJob) -> None:
        try:
            while True:
                await asyncio.sleep(max(1.0, self._s.benchmark_poll_seconds))
                if job.state != "running":
                    return
                if time.time() - job.started_at > self._s.benchmark_timeout_seconds:
                    job.state = "failed"
                    job.error = "测速超时，已停止本地跟踪（NAS 侧任务可能仍在跑，可手动停止）"
                    job.finished_at = time.time()
                    return

                try:
                    running = await self._nas._c.get(  # noqa: SLF001
                        "v1/storage/disk/benchmark/running", {"disk": job.disk}
                    )
                except UgosError as exc:
                    log.warning("测速进度查询失败：%s", exc)
                    continue

                if running:
                    job.progress = running
                    job.samples.append(
                        {
                            "ts": int(time.time()),
                            "read_mbps": ((running.get("read_perf") or {}).get("bw")),
                            "write_mbps": ((running.get("write_perf") or {}).get("bw")),
                        }
                    )
                else:
                    # running 变空 = 测试结束，取一次历史结果
                    job.state = "finished"
                    job.finished_at = time.time()
                    try:
                        info = await self._nas._c.get(  # noqa: SLF001
                            "v1/storage/disk/benchmark/info", {"disk": job.disk}
                        ) or {}
                        lst = info.get("detection_list") or []
                        job.result = lst[0] if lst else info
                    except UgosError as exc:
                        job.error = f"取测速结果失败：{exc}"
                    log.info("磁盘测速结束 %s", job.disk)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛给事件循环
            job.state = "failed"
            job.error = str(exc)
            job.finished_at = time.time()
            log.exception("测速轮询异常")

    # ---------------------------------------------------------------- 历史

    async def history(self, ref: str) -> dict:
        disk = await self._nas.resolve_disk(ref)
        dev = f"/dev/{disk.get('name')}"
        raw = await self._nas._c.get("v1/storage/disk/benchmark/info", {"disk": dev}) or {}
        items = []
        for d in raw.get("detection_list") or []:
            if not isinstance(d, dict):
                continue
            items.append(
                {
                    "at": d.get("time"),
                    "read_mbps": (d.get("read_perf") or {}).get("bw"),
                    "write_mbps": (d.get("write_perf") or {}).get("bw"),
                    "read_iops": (d.get("read_perf") or {}).get("iops"),
                    "write_iops": (d.get("write_perf") or {}).get("iops"),
                    "is_testing": d.get("isTesting"),
                    "raw": d,
                }
            )
        return {
            "disk": disk.get("name"),
            "dev": dev,
            "model": raw.get("model"),
            "serial": raw.get("serial"),
            "size": raw.get("size"),
            "items": items,
        }


class BenchmarkRejected(Exception):
    """NAS 侧拒绝测速（多为磁盘在用）。"""

    def __init__(self, message: str, hint: str = "") -> None:
        self.hint = hint
        super().__init__(message)
