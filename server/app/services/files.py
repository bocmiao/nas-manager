"""文件管理：浏览 / 下载 / 新建目录 / 重命名 / 删除。

实测契约（详见 docs/api-verified.md §七，全部在真机上验证过）：

- 根分类：`GET v2/filemgr/getRootDirs?client=pc` → `{root_list: [2,3,...]}`，
  **只有数字 root_type，没有对象**。数字与分类的对应来自前端 `initCache()`：
  1 收藏 / 2 个人文件夹 / 3 共享文件夹 / 4 用户文件夹 / 5 远程挂载 /
  6 外部设备 / 7 镜像(ISO) / 8 共享给我 / 9 标签
- 卷列表：`GET v1/filemgr/getVolumes` → `{result: [{name, path, fs_type, free, used, all}]}`
- 目录列表：`POST v2/filemgr/getDirFileListV2`
  - **根级**传 `root_type`（数字），**子路径**传 `path`（字符串），两者互斥；
    传 `path` 且路径不在可访问范围内 → `1301 不允许访问`（例如直接传 `/volume1`）
  - 共享根（root_type=3）需额外 `permission: 4`
  - 返回 `{left_tree: {...}, right_files: {total, top_path, permission_mask, files: [...]}}`
  - `top_path` 是树的根，不是当前目录；当前目录要自己记
  - 条目 `file_type`：**1=目录，0=文件**
  - 排序 `sort_type`：1 名称 / 2 大小 / 3 类型 / 4 修改时间 / 5 创建时间 / 6 访问时间 / 7 删除时间
- 下载三步（缺一不可）：
  1. `POST v2/filemgr/addPathsTask` `{paths:[{path, intranet_share_id}]}` → `{result: <task_uuid>}`
  2. `GET v2/filemgr/getDownloadToken?task_uuid=&coding=true` → `{dl_token}`
  3. `GET /ugreen/v2/filemgr/downloadFile?token=<dl_token>` → **原始字节**
     注意第 3 步用的是 dl_token，**不是**会话 token，前端也关掉了 tokenAuth
- 新建目录：`POST v2/filemgr/createFolder` `{path: <新目录完整路径>}`，
  `path` 是**目标目录本身**而不是父目录；已存在 → `1327 存在同名文件夹`
- 重命名：`POST v2/filemgr/rename` `{path, new_name}` → 返回更新后的条目
- 删除：`POST v2/filemgr/delPaths` `{paths: [<路径字符串>], forever: bool}`，
  **paths 必须是字符串数组**，传对象数组 → `9999 操作失败!`
- 剩余空间：`POST v1/filemgr/getPathFreeSize` `{path}` → `{result: <bytes>}`

只读取不到的信息不做猜测：条目的 `icon`/`icons` 是前端图标编号，这里只透传不解释。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import time
from urllib.parse import quote

from ..cache import TTLCache
from ..config import Settings
from ..ugos.client import UgosError
from .nas import NasService

log = logging.getLogger("files")

# 根分类（前端 initCache 的静态表，实测 root_list 会返回其中的子集）
ROOT_CATEGORIES: dict[int, tuple[str, str]] = {
    1: ("collect", "收藏"),
    2: ("personal", "个人文件夹"),
    3: ("common", "共享文件夹"),
    4: ("users", "用户文件夹"),
    5: ("network", "远程挂载"),
    6: ("device", "外部设备"),
    7: ("mirroring", "镜像"),
    8: ("share", "共享给我"),
    9: ("label", "标签"),
}

SORT_TYPES = {1: "name", 2: "size", 3: "type", 4: "mtime", 5: "ctime", 6: "atime", 7: "dtime"}
SORT_CODES = {v: k for k, v in SORT_TYPES.items()}

FILE_TYPE_DIR = 1
ROOT_TAG = 9     # 标签根：条目是标签定义，不是文件

# 上传行为（实测，见 docs/api-verified.md §七）
# 0/1/2 都会**静默覆盖**同名文件；3 会自动改名成 `名字(1).后缀`。
# 管理端默认用 3：宁可多一个文件，也不悄悄毁掉用户的同名文件。
UPLOAD_AUTO_RENAME = 3
UPLOAD_OVERWRITE = 1
UPLOAD_ACTION_LABELS = {0: "覆盖", 1: "覆盖", 2: "覆盖", 3: "同名自动改名"}

# 复制 / 移动的同名冲突策略（实测，见 docs/api-verified.md §四之九）
# 0=跳过 1=覆盖 3=另存(自动改名) 4=逐项询问
# 默认 3：目标已有同名时生成 `hello(1).txt`，不覆盖也不丢数据。
ACTION_TYPE_SKIP = 0
ACTION_TYPE_COVER = 1
ACTION_TYPE_SAVE = 3
ACTION_TYPE_ASK = 4

# 单次 POST 承载的字节数。NAS 侧不限制块大小（前端从 128KB 自增到 100MB），
# 这里取 8MB：既不会让网关超时，也不会把内存吃满。
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024

# 常见后缀归组，供客户端选图标；纯展示辅助，与 NAS 语义无关
EXT_GROUPS = {
    "image": {"jpg", "jpeg", "png", "gif", "webp", "heic", "bmp", "svg", "tiff", "avif"},
    "video": {"mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "ts", "m4v", "rmvb"},
    "audio": {"mp3", "flac", "wav", "aac", "m4a", "ogg", "ape", "wma"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "zst"},
    "document": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md",
                 "csv", "rtf", "epub", "mobi", "log"},
    "code": {"py", "js", "ts", "json", "yml", "yaml", "sh", "go", "rs", "java",
             "c", "cpp", "h", "swift", "kt", "html", "css", "sql", "toml", "ini"},
}
_EXT_TO_GROUP = {ext: g for g, exts in EXT_GROUPS.items() for ext in exts}


class FileRejected(Exception):
    """请求参数被本地规则拒绝（不发给 NAS）。"""

    def __init__(self, msg: str, code: int = 8001) -> None:
        super().__init__(msg)
        self.msg = msg
        self.code = code


def sanitize_path(path: str) -> str:
    """校验并规范化绝对路径。

    只做本地合法性检查（绝对路径、无 `..`、无空字节），**不做**越权判断——
    可见范围由 NAS 的权限模型决定（越界会返回 1301）。
    """
    if not path or not isinstance(path, str):
        raise FileRejected("path 不能为空")
    if "\x00" in path:
        raise FileRejected("path 含非法字符")
    if not path.startswith("/"):
        raise FileRejected("path 必须是绝对路径")
    # 先按原始分段判 `..`，再归一化。反过来做的话 `/volume1/../etc` 会被 normpath
    # 悄悄改写成 `/etc` 再转发出去——那等于帮调用方完成了一次路径逃逸。
    if ".." in path.split("/"):
        raise FileRejected("path 不允许包含 ..")
    norm = posixpath.normpath(path)
    if norm == "/":
        raise FileRejected("根目录请用 root_type 浏览，不要传 path=/")
    return norm


def _ext_group(ext: str) -> str:
    return _EXT_TO_GROUP.get(ext.lower(), "other")


def normalize_entry(raw: dict, root_type: int | None = None) -> dict:
    """把 NAS 条目裁成客户端需要的字段。原始条目约 30 个键，多数是前端内部状态。"""
    file_type = raw.get("file_type")
    is_dir = file_type == FILE_TYPE_DIR
    # root_type=9（标签）返回的是标签本身，没有 file_type，不能被当成 0 号“文件”
    is_tag = root_type == ROOT_TAG or "tag_id" in raw
    ext = (raw.get("ext") or "").lstrip(".")
    return {
        "name": raw.get("name") or "",
        "path": raw.get("path") or "",
        "is_dir": is_dir,
        "file_type": file_type,
        "size": int(raw.get("size") or 0),
        "ext": ext,
        "ext_group": "folder" if is_dir else _ext_group(ext),
        "mtime": int(raw.get("mtime") or 0),
        "ctime": int(raw.get("ctime") or 0),
        "permission_mask": int(raw.get("permission_mask") or 0),
        "icon": raw.get("icon"),
        "intranet_share_id": int(raw.get("intranet_share_id") or 0),
        "is_mount": bool((raw.get("mount") or {}).get("is_mount")),
        "recycle_status": raw.get("recycle_status"),
        # 该条目所在目录**是否支持回收站**。这是删除前必须看的字段：
        # 实测这台 NAS 上共享目录的 recycle_open 是 false，此时删文件是**直接永久删除**，
        # 根本不会进回收站（UGOS 网页端也是这么处理的：recycle_open=false 就强制 forever=true）。
        # 不透传这个字段，客户端就只能瞎承诺「可以找回」。
        "recycle_open": bool(raw.get("recycle_open")),
        "recycle_in_path": bool(raw.get("recycle_in_path")),
        # 回收站里剩余保留时间（秒/天由 NAS 侧决定），非回收站条目为 0
        "remaining_del_time": raw.get("remaining_del_time"),
        "is_tag": is_tag,
        "tag_id": raw.get("tag_id"),
        "tag_colour": raw.get("colour"),
    }


class FileService:
    def __init__(self, nas: NasService, cache: TTLCache, settings: Settings) -> None:
        self._nas = nas
        self._cache = cache
        self._s = settings

    @property
    def _c(self):
        return self._nas.client

    # ------------------------------------------------------------- 根与卷

    async def roots(self) -> dict:
        """可浏览的根分类 + 卷信息。分类只返回本机 root_list 里真实存在的那些。"""
        raw = await self._c.get("v2/filemgr/getRootDirs", {"client": "pc"})
        numbers = (raw or {}).get("root_list") or []
        if not isinstance(numbers, list):
            numbers = []
        categories = []
        for n in numbers:
            if not isinstance(n, int):
                continue
            key, label = ROOT_CATEGORIES.get(n, (f"root{n}", f"未知分类 {n}"))
            categories.append({"root_type": n, "key": key, "name": label})

        volumes = []
        try:
            vraw = await self._c.get("v1/filemgr/getVolumes")
            for v in (vraw or {}).get("result") or []:
                total = int(v.get("all") or 0)
                used = int(v.get("used") or 0)
                free = int(v.get("free") or 0)
                volumes.append({
                    "name": v.get("name"),
                    "path": v.get("path"),
                    "fs_type": v.get("fs_type"),
                    "total": total,
                    "used": used,
                    "free": free,
                    "used_ratio": round(used / total, 4) if total else None,
                })
        except UgosError as exc:
            # 卷信息是锦上添花，取不到不影响浏览
            log.warning("getVolumes 失败：%s %s", exc.code, exc.msg)

        return {"categories": categories, "volumes": volumes}

    # ------------------------------------------------------------- 列表

    async def list_dir(
        self,
        path: str | None = None,
        root_type: int | None = None,
        page: int = 1,
        limit: int = 200,
        sort: str = "name",
        reverse: bool = False,
    ) -> dict:
        """列目录。根级用 root_type，子路径用 path，二选一。"""
        if path and root_type is not None:
            raise FileRejected("path 与 root_type 只能给一个")
        if not path and root_type is None:
            raise FileRejected("path 与 root_type 至少给一个")

        page = max(1, int(page))
        limit = max(1, min(int(limit), 1000))
        sort_type = SORT_CODES.get(sort, 1)

        body: dict = {
            "limit": limit,
            "page": page,
            "is_shield_recycle": False,
            "data_type": 2,          # 2 = 只要右侧列表，不要左侧树
            "left_no_page_show": False,
            "left_count": 5000,
            "sort_type": sort_type,
            "reverse": bool(reverse),
            "intranet_share_id": 0,
            "permission": 0,
            "version": 1,
            "operate": 0,
        }
        if path:
            body["path"] = sanitize_path(path)
        else:
            body["root_type"] = int(root_type)
            # 共享文件夹根需要 permission=4，否则列不出来（见模块 docstring）
            if int(root_type) == 3:
                body["permission"] = 4

        data = await self._c.post("v2/filemgr/getDirFileListV2", body)
        right = (data or {}).get("right_files") or {}
        raws = right.get("files") or []
        entries = [normalize_entry(x, body.get("root_type")) for x in raws
                   if isinstance(x, dict)]

        total = int(right.get("total") or 0)
        return {
            "path": body.get("path"),
            "root_type": body.get("root_type"),
            "top_path": right.get("top_path") or "",
            "permission_mask": int(right.get("permission_mask") or 0),
            "page": page,
            "limit": limit,
            "total": total,
            "has_more": page * limit < total,
            "sort": SORT_TYPES.get(sort_type, "name"),
            "reverse": bool(reverse),
            "entries": entries,
        }

    # ------------------------------------------------------------- 下载

    async def download_token(self, path: str) -> dict:
        """走完前两步，拿到一次性下载令牌。第三步由路由层流式转发。"""
        clean = sanitize_path(path)
        task = await self._c.post(
            "v2/filemgr/addPathsTask",
            {"paths": [{"path": clean, "intranet_share_id": 0}]},
        )
        uuid = (task or {}).get("result")
        if not uuid:
            raise FileRejected("NAS 未返回下载任务号", code=9999)

        tok = await self._c.get(
            "v2/filemgr/getDownloadToken",
            {"task_uuid": str(uuid), "coding": "true", "is_livephoto": "false"},
        )
        token = (tok or {}).get("dl_token")
        if not token:
            raise FileRejected("NAS 未返回下载令牌", code=9999)
        return {"path": clean, "token": token, "task_uuid": str(uuid)}

    # ------------------------------------------------------------- 写操作

    async def create_folder(self, parent: str, name: str) -> dict:
        """建目录。`path` 传**新目录的完整路径**——把父目录传进去只会得到 1327。"""
        if not name or "/" in name or "\x00" in name:
            raise FileRejected("目录名不能为空且不能包含 /")
        if name in (".", ".."):
            raise FileRejected("目录名非法")
        parent_clean = sanitize_path(parent)
        target = posixpath.join(parent_clean, name)
        data = await self._c.post("v2/filemgr/createFolder", {"path": target})
        return normalize_entry(data or {})

    async def rename(self, path: str, new_name: str) -> dict:
        if not new_name or "/" in new_name or "\x00" in new_name:
            raise FileRejected("新名称不能为空且不能包含 /")
        if new_name in (".", ".."):
            raise FileRejected("新名称非法")
        clean = sanitize_path(path)
        data = await self._c.post("v2/filemgr/rename", {"path": clean, "new_name": new_name})
        return normalize_entry(data or {})

    async def thumbnail(
        self,
        path: str,
        width: int = 88,
        height: int = 88,
        file_type: int = 1,
        mtime: int = 0,
        ctime: int = 0,
        file_size: int = 0,
        retries: int = 6,
        delay: float = 0.8,
    ) -> tuple[bytes, str]:
        """取文件缩略图，返回 `(二进制, content-type)`。

        实测四个要点：
        1. 鉴权走 **query token**（不带 token 直接 `1010 Token不可为空`）
        2. 首次请求返回 **HTTP 423** + `thumbnail is generating asynchronizely`，
           需要重试；实测约 2~3 秒后转 200
        3. 返回的**统一是 JPEG**（即使原图是 PNG），所以别拿原图后缀去判断
        4. **`width` / `height` 会被完全忽略**。前端传的是固定预设
           `[16, 120, 88, 48]`，但实测传 16 和传 512 拿回来的是同一张
           **112×96 JPEG（约 1KB）**。所以这个接口只配做列表小图标，
           需要原图请走 `preview()`。

        生成后 NAS 侧会缓存，同一文件再请求直接 200。
        """
        clean = sanitize_path(path)
        # 传了也没用，但还是按前端的预设值传，避免非预期分支
        width = max(16, min(int(width), 1024))
        height = max(16, min(int(height), 1024))
        token = await self._c.session_token()
        params = {
            "path": clean,
            "type": int(file_type or 1),
            "size_type": 0,
            "mtime": int(mtime or 0),
            "ctime": int(ctime or 0),
            "file_size": int(file_size or 0),
            "intranet_share_id": 0,
            "width": width,
            "height": height,
            "token": token,
        }

        last_note = ""
        for _ in range(max(1, retries)):
            async with self._c.open_stream("v1/filemgr/thumbnail", params) as resp:
                if resp.status_code == 200:
                    data = await resp.aread()
                    if not data:
                        raise FileRejected("缩略图返回空内容")
                    ctype = resp.headers.get("content-type") or "image/jpeg"
                    return data, ctype
                last_note = (await resp.aread()).decode("utf-8", "replace")[:160]
                if resp.status_code != 423:
                    raise FileRejected(f"缩略图返回 HTTP {resp.status_code}：{last_note}")
            # 只有 423（正在生成）会走到这里
            await asyncio.sleep(delay)
        raise FileRejected(f"缩略图仍在生成中，稍后再试（{last_note}）")

    async def preview(
        self,
        path: str,
        file_type: int = 1,
        mtime: int = 0,
        ctime: int = 0,
        file_size: int = 0,
    ) -> tuple[bytes, str]:
        """原图字节通道（`GET v1/filemgr/mediaPlayer`），返回 `(二进制, content-type)`。

        跟 `download()` 拿到的内容一样，但**不用三步换 `dl_token`**，
        带会话 token 一次就取到。实测 1600×1200 PNG 源图 37334 字节，
        回来一字节不差，`content-type` 也照实回 `image/png`。

        用途是预览；真要落盘存档还是走 `download()`，那条才是官方下载语义。
        """
        clean = sanitize_path(path)
        token = await self._c.session_token()
        params = {
            "path": clean,
            "type": int(file_type or 1),
            "size_type": 0,
            "mtime": int(mtime or 0),
            "ctime": int(ctime or 0),
            "file_size": int(file_size or 0),
            "intranet_share_id": 0,
            "width": 1920,
            "height": 1080,
            "token": token,
        }
        async with self._c.open_stream("v1/filemgr/mediaPlayer", params) as resp:
            if resp.status_code != 200:
                note = (await resp.aread()).decode("utf-8", "replace")[:160]
                raise FileRejected(f"预览返回 HTTP {resp.status_code}：{note}")
            ctype = resp.headers.get("content-type") or "application/octet-stream"
            data = await resp.aread()
        if not data:
            raise FileRejected("预览返回空内容")
        return data, ctype

    async def copy_or_move(
        self,
        src: list[str],
        dst: str,
        move: bool = False,
        action_type: int = ACTION_TYPE_SAVE,
    ) -> dict:
        """复制 / 移动（`POST v2/filemgr/cpOrMvPath`），返回 `{path, task_id}`。

        实测五个要点：
        1. `src` 必须是**字符串路径数组**。传 `[{path, intranet_share_id}]`
           会直接得到 `9999 操作失败!`（这点和 `delPaths` 一致）。
        2. `type`：`true` = 移动，`false` = 复制。
        3. `action_type`：0=跳过 1=覆盖 3=另存 4=逐项询问。
           实测 3 在目标已有同名时生成 `hello(1).txt`、`hello(2).txt`，
           既不覆盖也不丢数据，所以作为默认。
        4. 返回 `{path: "", task_id: "<uuid>"}`，是**异步任务**。小文件 0.7 秒就完，
           移动更快（0.1 秒）。
        5. **没有公开的进度查询接口**：`getDelTaskInfo` 用 `task_uuid` / `task_id`
           查会回 `9999 操作失败!`，用 `uuid` 查回
           `1368 对象访问超时，您的任务不存在或已结束`。所以调用方只能轮询
           目标目录来判断完成，别去找任务接口。
        """
        if not src:
            raise FileRejected("src 不能为空")
        if not isinstance(src, list):
            raise FileRejected("src 必须是路径数组")
        clean_dst = sanitize_path(dst)
        clean_src = [sanitize_path(p) for p in src]
        # 目录不能移动到自己的子目录里，否则会无限递归
        for p in clean_src:
            if clean_dst == p or clean_dst.startswith(p + "/"):
                raise FileRejected(f"不能把 {p} 复制/移动到它自己的子目录")

        body = {
            "dst": clean_dst,
            "src": clean_src,
            "action_type": int(action_type),
            "type": bool(move),
            "intranet_share_id": 0,
        }
        raw = await self._c.post("v2/filemgr/cpOrMvPath", body) or {}
        return {
            "task_id": raw.get("task_id") or "",
            "dst": clean_dst,
            "src": clean_src,
            "move": bool(move),
        }

    async def recycle_conf(self) -> dict:
        """回收站开关状态（`GET v1/filemgr/getRecycleConf`）。

        实测本机返回 `{"desktop_recycle": true, "dir_recycle": false}`：
        目录级回收站是关的，此时删除**不会进回收站**，删完就没了。
        单条目的准确信息看条目自身的 `recycle_open`，这里只作整体提示用。
        """
        raw = await self._c.get("v1/filemgr/getRecycleConf") or {}
        desktop = bool(raw.get("desktop_recycle"))
        directory = bool(raw.get("dir_recycle"))
        return {
            "desktop_recycle": desktop,
            "dir_recycle": directory,
            # 两个都关时，删除一定不可恢复
            "recoverable": desktop or directory,
        }

    async def delete(self, paths: list[str], forever: bool = False) -> dict:
        """删除。`paths` 必须是字符串数组，传对象数组 NAS 会报 9999。

        注意：`forever=false` **不保证**进回收站——是否回收由条目所在目录的
        `recycle_open` 决定，该字段为 false 时删了就是永久删除。
        所以调用方要用条目上的 `recycle_open` 做提示，别对用户乱承诺。
        """
        if not paths:
            raise FileRejected("paths 不能为空")
        if not isinstance(paths, list):
            raise FileRejected("paths 必须是数组")
        cleaned = [sanitize_path(p) for p in paths if isinstance(p, str)]
        if len(cleaned) != len(paths):
            raise FileRejected("paths 里有非字符串项")
        data = await self._c.post(
            "v2/filemgr/delPaths", {"paths": cleaned, "forever": bool(forever)}
        )
        # 返回的 result 通常是空串而不是任务 id，别当成异步任务去轮询
        task = data.get("result") if isinstance(data, dict) else None
        return {
            "deleted": cleaned,
            "forever": bool(forever),
            "task": task if isinstance(task, str) and task else None,
        }

    # ------------------------------------------------------------- 上传

    async def upload(
        self,
        dest_dir: str,
        filename: str,
        total: int,
        chunks,
        action_type: int = UPLOAD_AUTO_RENAME,
    ) -> dict:
        """流式上传一个文件。

        协议是「参数全在 header + body 放裸字节」的分块续传：
        每块一次 `POST v1/filemgr/fileUploadV2`，`begin_size` 是本块在文件里的偏移，
        `current_size` 是本块长度，`size` 是文件总长。实测分块是**追加**语义。

        这里只做串行转发：不落盘、不在内存里攒整个文件。
        `chunks` 是异步字节迭代器（路由层直接传 `request.stream()`）。
        """
        clean_dir = sanitize_path(dest_dir)
        if not filename or "/" in filename or "\x00" in filename:
            raise FileRejected("文件名不能为空且不能包含 /")
        if filename in (".", ".."):
            raise FileRejected("文件名非法")
        if total <= 0:
            raise FileRejected("文件大小必须大于 0")
        if action_type not in UPLOAD_ACTION_LABELS:
            raise FileRejected("action_type 只支持 0/1/2/3")

        uid = hashlib.md5(filename.encode("utf-8")).hexdigest()
        offset = 0
        result_path = ""

        async for raw in chunks:
            if not raw:
                continue
            # 单块超过阈值就切开，让每个请求都保持可控大小
            pos = 0
            while pos < len(raw):
                piece = raw[pos: pos + UPLOAD_CHUNK_SIZE]
                pos += len(piece)
                if offset + len(piece) > total:
                    raise FileRejected(
                        f"收到的字节数超过声明的 {total}，可能是 Content-Length 不实"
                    )
                data = await self._upload_chunk(
                    clean_dir, filename, uid, total, piece, offset, action_type
                )
                path = (data or {}).get("result")
                if path:
                    result_path = path
                offset += len(piece)

        if offset != total:
            raise FileRejected(f"实际收到 {offset} 字节，与声明的 {total} 不一致")

        return {
            "path": result_path,
            "name": posixpath.basename(result_path) or filename,
            "dir": clean_dir,
            "size": total,
            "action_type": action_type,
            "action": UPLOAD_ACTION_LABELS[action_type],
        }

    async def _upload_chunk(
        self,
        dest_dir: str,
        filename: str,
        uid: str,
        total: int,
        piece: bytes,
        offset: int,
        action_type: int,
    ):
        # header 只能是 ASCII：file_name / dir 必须 URL 编码，
        # 否则中文名会直接编码失败（前端也是 encodeURIComponent）。
        param = {
            "uuid": uid,
            "file_name": quote(filename, safe=""),
            "action_type": action_type,
            "size": total,
            "current_size": len(piece),
            "resume": True,
            "dir": quote(dest_dir, safe=""),
            # 必须是 int64 unix 秒；传 "YYYY-MM-DD hh:mm:ss" 会被 Go 反序列化拒绝
            "change_time": int(time.time()),
            "is_live_photo": False,
            "first_request": False,
            "begin_size": offset,
        }
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{quote(filename, safe="")}"',
            "ug-param": json.dumps(param, ensure_ascii=True, separators=(",", ":")),
        }
        return await self._c.post_raw("v1/filemgr/fileUploadV2", piece, headers=headers)

    # ------------------------------------------------------------- 空间

    async def free_space(self, path: str) -> dict:
        clean = sanitize_path(path)
        data = await self._c.post("v1/filemgr/getPathFreeSize", {"path": clean})
        free = int((data or {}).get("result") or 0)
        return {"path": clean, "free": free}
