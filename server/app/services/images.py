"""镜像管理：列表 / 删除（单个 + 批量）/ 拉取（异步任务 + 进度）。

实测契约（详见 docs/api-verified.md「Docker 镜像」）：
- 本模块端点**全部 POST**，用 GET 一律返回 `9405`（不是 9404）
- `ShowLocalImageV2` 才是本地镜像；`ObtainAllImages` 是 Docker Hub 商店搜索（1165 条）
- 拉取是「发起 + 轮询列表」：拉取期间会出现 `<name>:<tag>:pull`、`status=2` 的临时条目，
  完成后条目变成 `<name>:<tag>`、`status=1`；服务端**不提供字节级进度**，`progress` 恒为 0
- `QueryAllContaners` 传 `{"list":[{imageId,imageRef}]}` 才能拿到引用该镜像的容器
- `DeleteImage` / `BatchDeleteImage` 对不存在的镜像同样返回 `successful`（幂等），
  所以删除前必须自己校验镜像是否存在
- `BatchDeleteImage` 的 `imageIds` 必须是**对象数组**，传字符串数组会报
  `cannot unmarshal string into Go struct field ...`
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..ugos.client import UgosError
from .nas import NasService

log = logging.getLogger("images")

# 本地镜像状态位：实测 1 = 可用，2 = 拉取中
IMAGE_STATE = {1: "ready", 2: "pulling"}

PAGE_SIZE = 200
MAX_PAGES = 20


@dataclass
class PullJob:
    name: str
    tag: str
    state: str = "starting"      # starting / pulling / succeeded / failed
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    polls: int = 0
    progress: int | None = None  # 服务端 progress 字段（实测恒 0）
    size: int | None = None
    error: str | None = None
    note: str = ""

    @property
    def ref(self) -> str:
        return f"{self.name}:{self.tag}"

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "name": self.name,
            "tag": self.tag,
            "state": self.state,
            "started_at": int(self.started_at),
            "finished_at": int(self.finished_at) if self.finished_at else None,
            "elapsed_seconds": int((self.finished_at or time.time()) - self.started_at),
            "progress": self.progress,
            "size": self.size,
            "error": self.error,
            "note": self.note,
            "polls": self.polls,
        }


class ImageManager:
    def __init__(self, nas: NasService, settings: Settings) -> None:
        self._nas = nas
        self._s = settings
        self._jobs: dict[str, PullJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 列表

    async def list_images(self, *, with_containers: bool = True) -> dict:
        raw = await self._fetch_raw()
        items = [self._normalize(x) for x in raw]

        if with_containers:
            ready = [x for x in items if x.get("state") == "ready"]
            try:
                refs = await self._container_refs(ready)
            except Exception as exc:  # noqa: BLE001 - 引用查询失败不影响列表
                log.warning("镜像关联容器查询失败：%s", exc)
                refs = {}
            for x in items:
                names = refs.get(x.get("id") or "", [])
                x["containers"] = names
                x["container_count"] = len(names)
                x["in_use"] = bool(names)

        return {
            "total": len(items),
            "ready": sum(1 for x in items if x["state"] == "ready"),
            "pulling": sum(1 for x in items if x["state"] == "pulling"),
            "items": items,
            "collected_at": int(time.time()),
        }

    async def _fetch_raw(self) -> list[dict]:
        """分页拉全量本地镜像（PageNum/PageSize 实测可选，显式传更稳）。"""
        out: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._nas.client.post(
                "v1/docker/image/ShowLocalImageV2",
                {"PageNum": page, "PageSize": PAGE_SIZE},
            ) or {}
            batch = [x for x in (data.get("result") or []) if isinstance(x, dict)]
            out.extend(batch)
            total = int(data.get("originalTotal") or 0)
            if not batch or len(out) >= total:
                break
        return out

    @staticmethod
    def _normalize(x: dict) -> dict:
        status = x.get("status")
        cid = x.get("imageId") or ""
        return {
            "id": cid,
            "id_short": cid[7:19] if cid.startswith("sha256:") else cid[:12],
            "ref": x.get("imageRef"),
            "name": x.get("imageName"),
            "tag": x.get("imageVersion"),
            "size": int(x.get("imageSize") or 0),
            "created_at": x.get("create"),
            "state": IMAGE_STATE.get(status, f"status_{status}"),
            "status": status,
            "progress": x.get("progress"),
            "error": x.get("pullErr") or None,
            "link": x.get("linkUrl") or None,
            "describe": x.get("imageDescribe") or None,
            "containers": [],
            "container_count": 0,
            "in_use": False,
        }

    async def _container_refs(self, images: list[dict]) -> dict[str, list[str]]:
        """批量查镜像被哪些容器引用；返回 {imageId: [容器名]}。"""
        mapping: dict[str, list[str]] = {}
        chunk = 50
        for i in range(0, len(images), chunk):
            part = images[i : i + chunk]
            payload = {"list": [{"imageId": x["id"], "imageRef": x["ref"]} for x in part]}
            data = await self._nas.client.post("v1/docker/image/QueryAllContaners", payload) or {}
            for row in data.get("result") or []:
                if isinstance(row, dict) and row.get("imageId"):
                    mapping[row["imageId"]] = row.get("names") or []
        return mapping

    # ---------------------------------------------------------------- 删除

    async def delete(self, refs: list[str], force: bool = False) -> dict:
        """删除镜像。refs 支持 imageId、imageRef（name:tag）、镜像名。

        `DeleteImage` 对不存在的镜像也返回 `successful`，被停止的容器引用的镜像
        Docker 会拒绝删除但 UGOS 仍回 `successful`，所以**删除后必须重新列表校验**，
        否则客户端会以为删掉了、刷新后又还在。
        `in_use` 非空时默认拒绝，需显式 `force=true`。
        """
        if not refs:
            raise ValueError("未指定要删除的镜像")

        listing = await self.list_images(with_containers=True)
        index: dict[str, dict] = {}
        for img in listing["items"]:
            for key in (img.get("id"), img.get("ref"), img.get("name")):
                if key:
                    index.setdefault(str(key), img)

        targets: list[dict] = []
        missing: list[str] = []
        for ref in refs:
            img = index.get(ref)
            if not img:
                missing.append(ref)
                continue
            if img["state"] != "ready":
                missing.append(ref)
                continue
            if all(img["id"] != t["id"] for t in targets):
                targets.append(img)

        if not targets:
            return {
                "requested": refs,
                "attempted": [],
                "deleted": [],
                "missing": missing,
                "mode": None,
                "in_use": [],
                "verified": True,
                "still_present": [],
                "server_result": None,
                "note": "没有可删除的镜像（不存在或仍在拉取中）",
            }

        in_use = [{"ref": t["ref"], "containers": t["containers"]}
                  for t in targets if t["in_use"]]
        if in_use and not force:
            detail = "；".join(
                f"{x['ref']}（被 {', '.join(x['containers'])} 引用）" for x in in_use
            )
            raise PermissionError(
                f"镜像正被容器引用，Docker 会拒绝删除：{detail}。确认要删请显式传 force=true"
            )

        # 单个走 DeleteImage，多个走 BatchDeleteImage（两套 body 结构不同）
        if len(targets) == 1:
            t = targets[0]
            body = {
                "id": t["id"],
                "status": t["status"],
                "imageName": t["name"],
                "imageVersion": t["tag"],
            }
            result = await self._nas.client.post("v1/docker/image/DeleteImage", body)
            mode = "single"
        else:
            body = {
                "imageIds": [
                    {
                        "id": t["id"],
                        "status": t["status"],
                        "imageName": t["name"],
                        "imageVersion": t["tag"],
                    }
                    for t in targets
                ]
            }
            result = await self._nas.client.post("v1/docker/image/BatchDeleteImage", body)
            mode = "batch"

        # 删除后校验：按 id 重新列一次，仍在列表里的就是没删掉
        verified: bool | None = None
        still: list[dict] = []
        verify_error: str | None = None
        try:
            after = await self._fetch_raw()
        except UgosError as exc:
            verify_error = f"{type(exc).__name__}: {exc}"
            log.warning("删除后复核镜像列表失败：%s", exc)
        else:
            remaining = {x.get("imageId") or "" for x in after}
            still = [
                {"ref": t["ref"], "id": t["id"], "containers": t["containers"]}
                for t in targets if t["id"] in remaining
            ]
            verified = not still

        still_refs = {x["ref"] for x in still}
        return {
            "requested": refs,
            "attempted": [t["ref"] for t in targets],
            "deleted": [t["ref"] for t in targets if t["ref"] not in still_refs],
            "missing": missing,
            "mode": mode,
            "in_use": in_use,
            "verified": verified,
            "still_present": still,
            "verify_error": verify_error,
            "server_result": result,
            "note": "UGOS 对不存在的镜像也返回 successful，本层以「删除后重新列表」为准；"
                    "verified=false 或 still_present 非空表示镜像其实还在（多为容器仍引用）",
        }

    # ---------------------------------------------------------------- 拉取

    def jobs(self) -> list[dict]:
        return [j.to_dict() for j in self._jobs.values()]

    def job_status(self, ref: str) -> dict | None:
        job = self._jobs.get(ref) or self._jobs.get(f"{ref}:latest")
        return job.to_dict() if job else None

    async def pull(self, name: str, tag: str = "latest") -> dict:
        name = (name or "").strip()
        tag = (tag or "latest").strip() or "latest"
        if not name or "://" in name or " " in name:
            raise ValueError("镜像名非法")
        if not self._s.image_allow_pull:
            raise PermissionError("服务端已禁用镜像拉取（IMAGE_ALLOW_PULL=false）")

        ref = f"{name}:{tag}"
        async with self._lock:
            job = self._jobs.get(ref)
            if job and job.state in ("starting", "pulling"):
                return job.to_dict()

            # 已存在就直接返回，避免无意义的等待与轮询
            try:
                existing = await self._fetch_raw()
                for x in existing:
                    if x.get("imageRef") == ref and x.get("status") == 1:
                        done = PullJob(name=name, tag=tag, state="succeeded",
                                       finished_at=time.time(), size=int(x.get("imageSize") or 0),
                                       note="本地已存在该镜像，未重复拉取")
                        self._jobs[ref] = done
                        return done.to_dict()
            except UgosError as exc:
                log.warning("拉取前查询本地镜像失败：%s", exc)

            try:
                await self._nas.client.post(
                    "v1/docker/image/DownloadImage", {"name": name, "version": tag}
                )
            except UgosError as exc:
                job = PullJob(name=name, tag=tag, state="failed", error=str(exc),
                              finished_at=time.time())
                self._jobs[ref] = job
                raise

            job = PullJob(name=name, tag=tag, state="pulling")
            self._jobs[ref] = job
            self._tasks[ref] = asyncio.create_task(self._poll(job))
            log.info("镜像拉取已发起 %s", ref)
            return job.to_dict()

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

    async def _poll(self, job: PullJob) -> None:
        try:
            while job.state == "pulling":
                await asyncio.sleep(max(1.0, self._s.image_pull_poll_seconds))
                job.polls += 1
                if time.time() - job.started_at > self._s.image_pull_timeout_seconds:
                    job.state = "failed"
                    job.error = "拉取超时，已停止本地跟踪（NAS 侧可能仍在下载）"
                    job.finished_at = time.time()
                    return

                try:
                    items = await self._fetch_raw()
                except UgosError as exc:
                    log.warning("拉取进度查询失败：%s", exc)
                    continue

                pulling = next((x for x in items if x.get("imageRef") == f"{job.ref}:pull"), None)
                done = next((x for x in items if x.get("imageRef") == job.ref), None)

                if pulling and pulling.get("pullErr"):
                    job.state = "failed"
                    job.error = str(pulling.get("pullErr"))
                    job.progress = pulling.get("progress")
                    job.finished_at = time.time()
                    return

                if pulling:
                    job.progress = pulling.get("progress")
                    continue

                if done and done.get("status") == 1:
                    job.state = "succeeded"
                    job.progress = done.get("progress")
                    job.size = int(done.get("imageSize") or 0)
                    job.finished_at = time.time()
                    log.info("镜像拉取完成 %s（%s B）", job.ref, job.size)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛给事件循环
            job.state = "failed"
            job.error = str(exc)
            job.finished_at = time.time()
            log.exception("镜像拉取轮询异常")
