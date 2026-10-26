#!/usr/bin/env python3
"""文件管理服务的机械验证（合成数据 + 假客户端，不碰 NAS）。

真机契约由 `scripts/probe_files.py` / `probe_files2.py` 验证；
这里只锁三件事：
1. **发给 NAS 的请求形状**——root_type 与 path 互斥、共享根要 permission=4、
   排序名到 sort_type 的映射、固定字段一个都不能少；
2. **返回归一化**——file_type 0/1 的语义、标签条目不能被当成文件、
   `top_path` 不参与"当前目录"判断；
3. **本地拒绝规则**——`..`、相对路径、空 path、path 与 root_type 同时给、
   `delPaths` 传对象等，都必须本地拦掉，不能发到 NAS 上试。

用法: python scripts/test_files.py
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from app.config import Settings  # noqa: E402
from app.services.files import (  # noqa: E402
    FileRejected,
    FileService,
    ROOT_CATEGORIES,
    SORT_CODES,
    UPLOAD_AUTO_RENAME,
    UPLOAD_CHUNK_SIZE,
    normalize_entry,
    sanitize_path,
)

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))


class FakeClient:
    """记录调用，返回预置数据。只覆盖被测接口。"""

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, str, object, object]] = []
        self.raw_calls: list[dict] = []
        self._responses = responses or {}

    def _record(self, method: str, path: str, payload) -> object:
        self.calls.append((method, path, payload, None))
        if path in self._responses:
            return self._responses[path]
        return {}

    async def get(self, path: str, params=None, **_kw):
        return self._record("GET", path, params)

    async def post(self, path: str, body=None, params=None, **_kw):
        return self._record("POST", path, body)

    async def post_raw(self, path: str, content: bytes, headers=None, **_kw):
        self.raw_calls.append({"path": path, "size": len(content), "headers": headers})
        return {"result": "/volume1/docker/上传结果.bin"}

    raw_calls: list = []


class FakeNas:
    def __init__(self, client: FakeClient) -> None:
        self.client = client


class FakeCache:
    async def get_or_set(self, _key, _ttl, fn):
        return await fn()


def svc(responses=None) -> tuple[FileService, FakeClient]:
    c = FakeClient(responses)
    return FileService(FakeNas(c), FakeCache(), Settings()), c


def last_body(c: FakeClient) -> dict:
    return c.calls[-1][2] or {}


# ---------------------------------------------------------------- 1. 路径校验

def test_sanitize() -> None:
    print("\n[1] 路径校验（本地拦截，不发 NAS）")
    ok = sanitize_path("/volume1/docker")
    check("正常绝对路径原样通过", ok == "/volume1/docker", ok)
    check("多余斜杠被规范化", sanitize_path("/volume1//docker/") == "/volume1/docker")
    check("路径内的 . 被消掉", sanitize_path("/volume1/./docker") == "/volume1/docker")
    check("中文路径保留", sanitize_path("/volume1/港剧/x.mkv") == "/volume1/港剧/x.mkv")

    for bad, why in [
        ("", "空字符串"),
        ("volume1/docker", "相对路径"),
        ("/", "裸根目录"),
        ("/volume1/../etc", "含 .."),
        ("/volume1/a\x00b", "含空字节"),
    ]:
        try:
            sanitize_path(bad)
            check(f"拒绝{why}", False, f"未拒绝 {bad!r}")
        except FileRejected:
            check(f"拒绝{why}", True)

    # 关键：`..` 必须在归一化【之前】按原始分段判掉。
    # 先 normpath 再判的话，`/volume1/../etc` 会变成 `/etc` 被静默转发出去。
    for sneaky in ["/volume1/../../etc/passwd", "/volume1/docker/../..", "/./../x"]:
        try:
            r = sanitize_path(sneaky)
            check(f"多级 .. 被拒：{sneaky}", False, f"未拒绝，归一化成了 {r}")
        except FileRejected:
            check(f"多级 .. 被拒：{sneaky}", True)


# ---------------------------------------------------------------- 2. 请求形状

def test_list_body() -> None:
    print("\n[2] 列表请求形状")

    async def run():
        s, c = svc({"v2/filemgr/getDirFileListV2": {"right_files": {"total": 0, "files": []}}})
        await s.list_dir(root_type=2)
        b = last_body(c)
        check("根级用 root_type 不用 path", "root_type" in b and "path" not in b, str(b.get("root_type")))
        for key, want in [
            ("limit", 200), ("page", 1), ("data_type", 2), ("sort_type", 1),
            ("reverse", False), ("intranet_share_id", 0), ("permission", 0),
            ("version", 1), ("operate", 0),
            ("is_shield_recycle", False), ("left_no_page_show", False), ("left_count", 5000),
        ]:
            check(f"固定字段 {key}={want}", b.get(key) == want, repr(b.get(key)))

        await s.list_dir(root_type=3)
        check("共享根自动带 permission=4", last_body(c).get("permission") == 4,
              repr(last_body(c).get("permission")))

        await s.list_dir(root_type=4)
        check("非共享根 permission=0", last_body(c).get("permission") == 0)

        await s.list_dir(path="/volume1/docker")
        b = last_body(c)
        check("子路径用 path 不用 root_type", "path" in b and "root_type" not in b, str(b.get("path")))

        await s.list_dir(path="/volume1/docker", sort="mtime", reverse=True)
        check("sort=mtime → sort_type=4", last_body(c).get("sort_type") == 4)
        check("reverse 透传", last_body(c).get("reverse") is True)

        await s.list_dir(path="/volume1/docker", limit=9999)
        check("limit 上限截到 1000", last_body(c).get("limit") == 1000, repr(last_body(c).get("limit")))
        await s.list_dir(path="/volume1/docker", page=0)
        check("page 下限抬到 1", last_body(c).get("page") == 1)

    asyncio.run(run())
    check("全部排序名都有映射", set(SORT_CODES) == {"name", "size", "type", "mtime", "ctime", "atime", "dtime"})


def test_list_rejects() -> None:
    print("\n[3] 列表本地拒绝")

    async def run():
        s, c = svc()
        for kwargs, why in [
            ({"path": "/a", "root_type": 2}, "path 与 root_type 同时给"),
            ({}, "两者都不给"),
            ({"path": "relative/path"}, "相对路径"),
            ({"path": "/volume1/../etc"}, "含 .."),
        ]:
            before = len(c.calls)
            try:
                await s.list_dir(**kwargs)
                check(f"拒绝{why}", False, "未拒绝")
            except FileRejected:
                check(f"拒绝{why}", True)
            check(f"{why} 未发请求到 NAS", len(c.calls) == before)

    asyncio.run(run())


# ---------------------------------------------------------------- 3. 归一化

def test_normalize() -> None:
    print("\n[4] 条目归一化")

    folder = normalize_entry({
        "path": "/volume1/docker/backup", "name": "backup", "file_type": 1,
        "size": 0, "ext": "", "mtime": 1792943409, "intranet_share_id": 0,
        "mount": {"is_mount": False},
    })
    check("file_type=1 是目录", folder["is_dir"] is True)
    check("目录 ext_group=folder", folder["ext_group"] == "folder")

    plain = normalize_entry({
        "path": "/a/b.log", "name": "b.log", "file_type": 0, "size": 1592,
        "ext": "log", "mtime": 1,
    })
    check("file_type=0 是文件（不是未知）", plain["is_dir"] is False)
    check("文件 ext_group 按后缀归组", plain["ext_group"] == "document", plain["ext_group"])

    for ext, want in [("mkv", "video"), ("jpg", "image"), ("flac", "audio"),
                      ("7z", "archive"), ("py", "code"), ("rs", "code"),
                      ("weirdext", "other"), ("", "other")]:
        got = normalize_entry({"name": f"x.{ext}", "file_type": 0, "ext": ext})["ext_group"]
        check(f"后缀 {ext or '(空)'} → {want}", got == want, got)

    tag = normalize_entry({"tag_id": 15, "name": "个人", "colour": "#FFFFFF"}, root_type=9)
    check("标签条目 is_tag=True", tag["is_tag"] is True)
    check("标签能取到 tag_id", tag["tag_id"] == 15)
    check("标签带颜色", tag["tag_colour"] == "#FFFFFF")

    # root_type=9 下即使条目带 file_type=0，也必须标成标签
    tag2 = normalize_entry({"tag_id": 3, "name": "x", "file_type": 0}, root_type=9)
    check("标签根下 file_type=0 仍标记为标签", tag2["is_tag"] is True)

    # 缺字段不能炸
    shallow = normalize_entry({"name": "x"})
    check("缺字段条目可归一化", shallow["size"] == 0 and shallow["is_dir"] is False)

    check("ROOT_CATEGORIES 覆盖 1..9",
          all(i in ROOT_CATEGORIES for i in range(1, 10)),
          str(sorted(ROOT_CATEGORIES)))


def test_listing_shape() -> None:
    print("\n[5] 列表返回结构")

    async def run():
        s, _c = svc({"v2/filemgr/getDirFileListV2": {
            "left_tree": {"type": 0},
            "right_files": {
                "total": 2, "top_path": "/home/user", "permission_mask": 7,
                "files": [
                    {"name": "Music", "path": "/home/user/Music", "file_type": 1},
                    {"name": "a.txt", "path": "/home/user/a.txt", "file_type": 0, "size": 5},
                ],
            },
        }})
        r = await s.list_dir(path="/home/user")
        check("total 取 right_files.total", r["total"] == 2)
        check("top_path 透传但不当当前目录",
              r["top_path"] == "/home/user" and r["path"] == "/home/user")
        check("反过来：子目录时 path 与 top_path 不同",
              (await s.list_dir(path="/home/user/Music"))["path"] == "/home/user/Music")
        check("permission_mask 透传", r["permission_mask"] == 7)
        check("entries 数量正确", len(r["entries"]) == 2)
        check("has_more 按 page*limit<total 计算",
              (await s.list_dir(path="/x", page=2, limit=100))["has_more"] is False)

    asyncio.run(run())


# ---------------------------------------------------------------- 4. 写操作

def test_writes() -> None:
    print("\n[6] 写操作请求形状")

    async def run():
        s, c = svc({
            "v2/filemgr/createFolder": {"name": "new", "path": "/volume1/docker/new", "file_type": 1},
            "v2/filemgr/rename": {"name": "b", "path": "/volume1/docker/b", "file_type": 0},
        })

        await s.create_folder("/volume1/docker", "new")
        b = last_body(c)
        check("createFolder.path 是【新目录完整路径】",
              b.get("path") == "/volume1/docker/new", str(b.get("path")))
        check("createFolder 不传 name 字段", "name" not in b, str(sorted(b)))

        for bad in ["", "a/b", "..", "."]:
            try:
                await s.create_folder("/volume1/docker", bad)
                check(f"拒绝目录名 {bad!r}", False, "未拒绝")
            except FileRejected:
                check(f"拒绝目录名 {bad!r}", True)

        await s.rename("/volume1/docker/a", "b")
        b = last_body(c)
        check("rename 传 path + new_name",
              b.get("path") == "/volume1/docker/a" and b.get("new_name") == "b", str(b))

        await s.delete(["/volume1/docker/a", "/volume1/docker/b"])
        b = last_body(c)
        check("delPaths.paths 是字符串数组",
              b.get("paths") == ["/volume1/docker/a", "/volume1/docker/b"], str(b.get("paths")))
        check("delPaths 默认 forever=False（进回收站）", b.get("forever") is False)

        await s.delete(["/a"], forever=True)
        check("forever 可置 True", last_body(c).get("forever") is True)

        # 关键：传对象数组必须被本地拦掉，否则 NAS 只回 9999，无从定位
        for bad, why in [
            ([{"path": "/a"}], "对象数组"),
            ([], "空数组"),
            (["/a", 123], "混入非字符串"),
            (["relative"], "相对路径"),
        ]:
            before = len(c.calls)
            try:
                await s.delete(bad)
                check(f"delPaths 拒绝{why}", False, "未拒绝")
            except FileRejected:
                check(f"delPaths 拒绝{why}", True)
            check(f"delPaths {why} 未发请求", len(c.calls) == before)

        await s.free_space("/volume1/docker")
        check("getPathFreeSize 传 path",
              c.calls[-1][1] == "v1/filemgr/getPathFreeSize" and last_body(c).get("path") == "/volume1/docker")

    asyncio.run(run())


# ---------------------------------------------------------------- 5. 下载

def test_download() -> None:
    print("\n[7] 下载链路")

    async def run():
        s, c = svc({
            "v2/filemgr/addPathsTask": {"result": "abc123"},
            "v2/filemgr/getDownloadToken": {"dl_token": "tok-xyz"},
        })
        info = await s.download_token("/volume1/docker/backup/alerts.log")
        add_call = next(x for x in c.calls if x[1].endswith("addPathsTask"))
        tok_call = next(x for x in c.calls if x[1].endswith("getDownloadToken"))
        check("第一步 addPathsTask 用对象数组",
              (add_call[2]["paths"][0]) == {"path": "/volume1/docker/backup/alerts.log",
                                            "intranet_share_id": 0},
              str(add_call[2]))
        check("第二步 task_uuid 透传", tok_call[2].get("task_uuid") == "abc123")
        check("第二步 coding=true", tok_call[2].get("coding") == "true")
        check("拿到 dl_token", info["token"] == "tok-xyz")
        check("路径已规范化", info["path"] == "/volume1/docker/backup/alerts.log")

        # 第一步没给 task uuid → 必须报错，不能带着空 token 往下走
        s2, _ = svc({"v2/filemgr/addPathsTask": {}})
        try:
            await s2.download_token("/a")
            check("缺 task_uuid 时报错", False, "未报错")
        except FileRejected:
            check("缺 task_uuid 时报错", True)

        # 第二步没给 dl_token
        s3, _ = svc({"v2/filemgr/addPathsTask": {"result": "x"},
                     "v2/filemgr/getDownloadToken": {}})
        try:
            await s3.download_token("/a")
            check("缺 dl_token 时报错", False, "未报错")
        except FileRejected:
            check("缺 dl_token 时报错", True)

    asyncio.run(run())


# ---------------------------------------------------------------- 6. 上传

async def _drain(data: bytes, pieces: int = 1):
    step = max(1, len(data) // pieces)
    for i in range(0, len(data), step):
        yield data[i: i + step]


def test_upload() -> None:
    print("\n[7] 上传请求形状")

    async def run():
        s, c = svc()
        c.raw_calls = []
        payload = bytes(range(256)) * 40      # 10240 字节
        res = await s.upload("/volume1/docker/backup", "文件 名.bin", len(payload),
                             _drain(payload, 4))

        check("返回体大小正确", res["size"] == len(payload), str(res["size"]))
        check("默认 action_type=3（同名自动改名）", res["action_type"] == UPLOAD_AUTO_RENAME,
              str(res["action_type"]))
        check("默认动作文案说明不覆盖", "改名" in res["action"], res["action"])

        calls = c.raw_calls
        check("打到 v1/filemgr/fileUploadV2（v1 不是 v2）",
              all(x["path"] == "v1/filemgr/fileUploadV2" for x in calls),
              str({x["path"] for x in calls}))

        import json as _json
        params = [_json.loads(x["headers"]["ug-param"]) for x in calls]
        want_keys = {"uuid", "file_name", "action_type", "size", "current_size",
                     "resume", "dir", "change_time", "is_live_photo",
                     "first_request", "begin_size"}
        check("ug-param 字段齐全且不多不少", set(params[0]) == want_keys,
              str(sorted(set(params[0]) ^ want_keys)))

        check("各块 size 恒为总长", all(x["size"] == len(payload) for x in params),
              str([x["size"] for x in params]))
        check("begin_size 从 0 递增且首尾相接",
              [x["begin_size"] for x in params] ==
              [0] + [x["begin_size"] + x["current_size"] for x in params[:-1]],
              str([(x["begin_size"], x["current_size"]) for x in params]))
        check("current_size 之和等于总长",
              sum(x["current_size"] for x in params) == len(payload))
        check("change_time 是 int 不是字符串",
              all(isinstance(x["change_time"], int) for x in params),
              str(type(params[0]["change_time"])))
        check("file_name 已 URL 编码（无中文）",
              all(x["file_name"].isascii() for x in params), params[0]["file_name"])
        check("dir 已 URL 编码（无中文）",
              all(x["dir"].isascii() for x in params), params[0]["dir"])
        check("uuid 是文件名 md5（32 位小写 hex）",
              len(params[0]["uuid"]) == 32 and params[0]["uuid"].islower(),
              params[0]["uuid"])
        check("所有块 uuid 一致",
              len({x["uuid"] for x in params}) == 1)
        check("Content-Type 是八位字节流",
              all(x["headers"]["Content-Type"] == "application/octet-stream"
                  for x in calls))
        check("Content-Disposition 格式正确",
              all(x["headers"]["Content-Disposition"].startswith('attachment; filename="')
                  for x in calls), calls[0]["headers"]["Content-Disposition"])
        check("ug-param 是单行 JSON（无换行）",
              all("\n" not in x["headers"]["ug-param"] for x in calls))

        # 单块超过阈值要被切开
        c.raw_calls = []
        big = b"x" * (UPLOAD_CHUNK_SIZE * 2 + 100)
        await s.upload("/volume1/docker", "big.bin", len(big), _drain(big, 1))
        check("超过 8MB 的单块被切成多块", len(c.raw_calls) == 3, str(len(c.raw_calls)))
        check("切块后总字节仍一致",
              sum(x["size"] for x in c.raw_calls) == len(big))

        # 本地拒绝
        for args, why in [
            (("/volume1/docker", "a/b", 10, None, 3), "文件名带 /"),
            (("/volume1/docker", "", 10, None, 3), "文件名为空"),
            (("/volume1/docker", "a.txt", 0, None, 3), "大小为 0"),
            (("/volume1/docker", "a.txt", 10, None, 9), "非法 action_type"),
            (("relative", "a.txt", 10, None, 3), "目标目录是相对路径"),
        ]:
            c.raw_calls = []
            try:
                await s.upload(*args)
                check(f"上传拒绝{why}", False, "未拒绝")
            except FileRejected:
                check(f"上传拒绝{why}", True)
            check(f"上传{why} 未发请求", len(c.raw_calls) == 0)

        # 声明大小与实际不符
        c.raw_calls = []
        try:
            await s.upload("/volume1/docker", "a.txt", 100, _drain(b"x" * 10, 1))
            check("声明大小大于实际要报错", False, "未报错")
        except FileRejected:
            check("声明大小大于实际要报错", True)

        c.raw_calls = []
        try:
            await s.upload("/volume1/docker", "a.txt", 5, _drain(b"x" * 10, 1))
            check("实际大于声明要报错", False, "未报错")
        except FileRejected:
            check("实际大于声明要报错", True)

    asyncio.run(run())


# ---------------------------------------------------------------- 7. 根分类

def test_roots() -> None:
    print("\n[8] 根分类与卷")

    async def run():
        s, c = svc({
            "v2/filemgr/getRootDirs": {"root_list": [2, 3, 4, 6, 9]},
            "v1/filemgr/getVolumes": {"result": [
                {"name": "存储空间1", "path": "/volume1", "fs_type": "ext4",
                 "free": 4368074858496, "used": 3536457715712, "all": 7904532574208},
            ]},
        })
        r = await s.roots()
        check("分类数量与 root_list 一致", len(r["categories"]) == 5, str(len(r["categories"])))
        check("数字映射到中文名",
              [x["name"] for x in r["categories"]] ==
              ["个人文件夹", "共享文件夹", "用户文件夹", "外部设备", "标签"],
              str([x["name"] for x in r["categories"]]))
        check("分类带 key 供前端选图标",
              [x["key"] for x in r["categories"]] ==
              ["personal", "common", "users", "device", "label"])
        v = r["volumes"][0]
        check("卷已用比例计算", abs(v["used_ratio"] - 0.4474) < 0.001, str(v["used_ratio"]))
        check("卷字段齐全", {"name", "path", "fs_type", "total", "used", "free"} <= set(v))

        # 未知 root_type 不能丢，要给可读占位
        s2, _ = svc({"v2/filemgr/getRootDirs": {"root_list": [2, 77]}})
        r2 = await s2.roots()
        check("未知 root_type 保留并给占位名",
              any(x["root_type"] == 77 and "未知" in x["name"] for x in r2["categories"]),
              str(r2["categories"]))

        # 卷接口挂了不影响分类
        s3, c3 = svc({"v2/filemgr/getRootDirs": {"root_list": [2]}})
        orig_get = c3.get

        async def flaky_get(path, params=None, **kw):
            if path == "v1/filemgr/getVolumes":
                from app.ugos.client import UgosError
                raise UgosError(9999, "挂了", path, "")
            return await orig_get(path, params, **kw)

        c3.get = flaky_get  # type: ignore[method-assign]
        r3 = await s3.roots()
        check("卷接口失败时分类仍返回", len(r3["categories"]) == 1 and r3["volumes"] == [])

    asyncio.run(run())


def main() -> int:
    test_sanitize()
    test_list_body()
    test_list_rejects()
    test_normalize()
    test_listing_shape()
    test_writes()
    test_download()
    test_upload()
    test_roots()

    failed = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 50)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("\n失败项：")
        for _s, name, detail in failed:
            print(f"  - {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
