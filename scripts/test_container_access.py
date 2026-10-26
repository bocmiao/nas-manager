#!/usr/bin/env python3
"""容器 Web 访问地址拼装的离线测试（不连 NAS）。

覆盖 `NasService.container_access_link` / `container_web_access` 的解析与拼装，
重点盯住实测踩过的几个点：
- `nasPort` 可能是字符串 "666" 也可能是数字 8090
- UDP 端口不可能承载 Web UI，必须排除
- `urlPath` 缺前导斜杠时要补
- `httpsEnabled` 为真时 https 端口可能和 http 不同
- IPv6 字面量必须加方括号
- 访问规则接口失败时不能连 http 地址一起丢掉
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from app.services.nas import NasService  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{name}{(' — ' + extra) if extra else ''}")


def eq(name: str, actual, expected) -> None:
    check(name, actual == expected, f"实际={actual!r} 期望={expected!r}")


class FakeClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path: str, params: dict | None = None, **_kw):
        return await self._respond(path, params)

    async def post(self, path: str, body: dict | None = None, params: dict | None = None, **_kw):
        return await self._respond(path, params if params is not None else body)

    async def _respond(self, path: str, params: dict | None):
        self.calls.append((path, dict(params or {})))
        if path not in self.responses:
            raise AssertionError(f"未预期的接口调用：{path}")
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value


class FakeCache:
    async def get_or_set(self, _key, _ttl, factory):
        return await factory()

    def invalidate(self, _key) -> None:
        return None


def make_service(responses: dict, host: str = "192.0.2.10") -> tuple[NasService, FakeClient]:
    client = FakeClient(responses)
    settings = SimpleNamespace(ugos_host=host, ugos_port=9443, ttl_docker=5)
    return NasService(client, FakeCache(), settings), client


DETAIL_TWO_PORTS = {
    "containerName": "demo-app",
    "runContainer": True,
    "imageName": "demo:1.0",
    "portMapping": [
        {"nasPort": "8090", "containerPort": 80, "portType": "tcp"},
        {"nasPort": 5353, "containerPort": 53, "portType": "udp"},
        {"nasPort": 0, "containerPort": 0, "portType": "tcp"},
        {"nasPort": 70000, "containerPort": 1, "portType": "tcp"},
    ],
}

LINK_8090 = {
    "rules": [
        {
            "linkType": 1,
            "accessType": 4,
            "dockerAccess": {"port": "8090"},
            "portAppAccess": None,
            "isAuthRequired": False,
            "disableRemoteAccess": None,
        },
        {
            "linkType": 2,
            "accessType": 1,
            "portAppAccess": {
                "port": "8090",
                "httpsEnable": True,
                "httpsPort": "8443",
                "urlPath": "admin",
            },
            "dockerAccess": None,
            "isAuthRequired": True,
            "disableRemoteAccess": None,
        },
    ],
    "openType": "",
    "supports": ["pc", "app"],
}


async def test_access_link() -> None:
    service, client = make_service({"v1/docker/container/GetContainerAccessLinkInfo": LINK_8090})
    data = await service.container_access_link(8090)

    eq("access_link 端口透传", data["port"], 8090)
    eq("access_link rules 条数", len(data["rules"]), 2)
    eq("access_link supports", data["supports"], ["pc", "app"])
    eq("access_link 请求参数名是 port", client.calls[0][1], {"port": 8090})

    first = data["rules"][0]
    eq("rule0 link_type", first["link_type"], 1)
    eq("rule0 access_type", first["access_type"], 4)
    eq("rule0 端口来自 dockerAccess", first["port"], "8090")
    eq("rule0 无 portAppAccess 时 https 为假", first["https_enabled"], False)

    second = data["rules"][1]
    eq("rule1 https 开启", second["https_enabled"], True)
    eq("rule1 https 端口", second["https_port"], "8443")
    eq("rule1 url_path", second["url_path"], "admin")
    eq("rule1 需要鉴权", second["auth_required"], True)


async def test_access_link_rejects_bad_port() -> None:
    service, _ = make_service({"v1/docker/container/GetContainerAccessLinkInfo": {}})
    for bad in (0, -1, 65536):
        try:
            await service.container_access_link(bad)
        except ValueError:
            check(f"access_link 拒绝端口 {bad}", True)
        else:
            check(f"access_link 拒绝端口 {bad}", False, "未抛 ValueError")


async def test_web_access_full() -> None:
    service, _ = make_service(
        {
            "v1/docker/container/GetContainerById": DETAIL_TWO_PORTS,
            "v1/docker/container/GetContainerAccessLinkInfo": LINK_8090,
        }
    )
    data = await service.container_web_access("a" * 32)

    eq("web_access 名称", data["container_name"], "demo-app")
    eq("web_access running", data["running"], True)
    eq("web_access host", data["host"], "192.0.2.10")
    eq("web_access 只剩 1 个 TCP 端口", len(data["ports"]), 1)

    port = data["ports"][0]
    eq("端口号（字符串被转成 int）", port["nas_port"], 8090)
    eq("容器内端口", port["container_port"], 80)
    eq("协议", port["protocol"], "tcp")
    eq("url_path 补了前导斜杠", port["url_path"], "/admin")
    eq("https 已识别", port["https_enabled"], True)
    eq(
        "候选地址（http + https，均带路径）",
        port["urls"],
        ["http://192.0.2.10:8090/admin", "https://192.0.2.10:8443/admin"],
    )
    check("注明了需要实际探测", "探测" in data["note"])


async def test_web_access_no_https_no_path() -> None:
    detail = {
        "containerName": "plain",
        "runContainer": True,
        "portMapping": [{"nasPort": 8080, "containerPort": 8080, "portType": "tcp"}],
    }
    link = {"rules": [{"accessType": 4, "dockerAccess": {"port": "8080"}}]}
    service, _ = make_service(
        {
            "v1/docker/container/GetContainerById": detail,
            "v1/docker/container/GetContainerAccessLinkInfo": link,
        }
    )
    data = await service.container_web_access("b" * 32)
    port = data["ports"][0]
    eq("仅 http 一条候选", len(port["urls"]), 1)
    eq("根路径", port["urls"][0], "http://192.0.2.10:8080/")
    eq("url_path 为空", port["url_path"], "")
    eq("https 未开启", port["https_enabled"], False)


async def test_web_access_rule_failure_keeps_http() -> None:
    detail = {
        "containerName": "x",
        "runContainer": True,
        "portMapping": [{"nasPort": 9000, "containerPort": 9000, "portType": "tcp"}],
    }
    service, _ = make_service(
        {
            "v1/docker/container/GetContainerById": detail,
            "v1/docker/container/GetContainerAccessLinkInfo": RuntimeError("boom"),
        }
    )
    data = await service.container_web_access("c" * 32)
    eq("规则失败仍返回一条 http 地址", len(data["ports"]), 1)
    eq("地址正确", data["ports"][0]["urls"], ["http://192.0.2.10:9000/"])
    eq("规则失败时 https 按未开启处理", data["ports"][0]["https_enabled"], False)


async def test_web_access_ipv6_host() -> None:
    detail = {
        "containerName": "v6",
        "runContainer": True,
        "portMapping": [{"nasPort": 8080, "containerPort": 80, "portType": "tcp"}],
    }
    service, _ = make_service(
        {
            "v1/docker/container/GetContainerById": detail,
            "v1/docker/container/GetContainerAccessLinkInfo": {"rules": []},
        },
        host="2001:db8::10",
    )
    data = await service.container_web_access("d" * 32)
    eq("IPv6 加方括号", data["ports"][0]["urls"], ["http://[2001:db8::10]:8080/"])


async def test_web_access_no_ports() -> None:
    detail = {"containerName": "empty", "runContainer": False, "portMapping": []}
    service, _ = make_service({"v1/docker/container/GetContainerById": detail})
    data = await service.container_web_access("e" * 32)
    eq("无端口时列表为空", data["ports"], [])
    eq("running 透传", data["running"], False)


async def test_containers_include_nas_ips() -> None:
    list_body = {
        "result": [
            {
                "containerId": "f" * 32,
                "name": "listed",
                "imageName": "img",
                "status": "running",
            }
        ]
    }
    detail_body = {
        "result": [
            {
                "containerId": "f" * 32,
                "status": "running",
                "portMapping": [{"nasPort": 8100, "containerPort": 8100, "portType": "tcp"}],
                "nasIPs": ["192.0.2.10", None, "192.0.2.11"],
            }
        ]
    }
    service, _ = make_service(
        {
            "v1/docker/container/ContainerListV2": list_body,
            "v1/docker/container/ShowContainerDetailListV2": detail_body,
        }
    )
    items = await service.containers()
    eq("容器数", len(items), 1)
    eq("端口已合并", items[0]["ports"], [{"nasPort": 8100, "containerPort": 8100, "portType": "tcp"}])
    eq("nas_ips 过滤掉非字符串", items[0]["nas_ips"], ["192.0.2.10", "192.0.2.11"])


async def main() -> int:
    await test_access_link()
    await test_access_link_rejects_bad_port()
    await test_web_access_full()
    await test_web_access_no_https_no_path()
    await test_web_access_rule_failure_keeps_http()
    await test_web_access_ipv6_host()
    await test_web_access_no_ports()
    await test_containers_include_nas_ips()

    print(f"合计 {PASS + FAIL} 项，通过 {PASS}，失败 {FAIL}")
    for line in FAILURES:
        print(f"  FAIL {line}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
