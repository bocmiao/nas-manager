#!/usr/bin/env python3
"""NAS 轻量哨兵 —— 只依赖 Python 标准库，不需要 FastAPI / httpx。

存在的理由：中间层跑在 NAS 自己身上，NAS 整机挂了中间层也一起消失，
「NAS 挂了」这件事它自己报不出来。所以需要一个跑在**别处**的小脚本，
从外部盯着，出事时通过 webhook 喊人。

检查项
  1. 中间层 `/health`（HTTP/HTTPS 均可，自签证书不校验）
  2. NAS 本身 9443 端口能否完成 TLS 握手（中间层已死但机器还活着时能区分开）
  3. `/health` 里 `ugos.reachable` / `last_poll_ok`（中间层活着但 UGOS 会话掉了）

只在「状态跳变」时告警（good→bad 报故障，bad→good 报恢复），不会刷屏。

用法
  # 最简：盯中间层
  python sentinel.py --url http://192.0.2.10:8787/health --webhook https://example.com/hook

  # 同时盯 NAS 端口
  python sentinel.py --url http://192.0.2.10:8787/health \\
      --nas 192.0.2.10:9443 --webhook https://example.com/hook

  # 每 60 秒一次，连续 3 次失败才告警，只跑一次后退出
  python sentinel.py ... --interval 60 --retries 3 --once

  # 全部参数都可用环境变量给（SENTINEL_URL / SENTINEL_WEBHOOK ...）
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"
STATE_PATH = os.path.join(
    os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp", "nas-sentinel-state.json"
)


# ---------------------------------------------------------------- 参数

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="NAS 轻量哨兵：轮询中间层 / NAS 端口，异常时打 webhook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="所有参数都对应一个 SENTINEL_* 环境变量，命令行优先。",
    )
    p.add_argument("--url", default=_env("SENTINEL_URL"),
                   help="中间层 health 地址，如 http://192.0.2.10:8787/health")
    p.add_argument("--nas", default=_env("SENTINEL_NAS"),
                   help="NAS 端口直连检查，格式 host:port，如 192.0.2.10:9443")
    p.add_argument("--webhook", default=_env("SENTINEL_WEBHOOK"),
                   help="告警 webhook URL（POST）。留空只打印到 stdout")
    p.add_argument("--webhook-format", default=_env("SENTINEL_WEBHOOK_FORMAT", "json"),
                   choices=("json", "text"), help="json=POST JSON 体；text=POST 纯文本")
    p.add_argument("--webhook-header", action="append", default=[],
                   metavar="K:V", help="追加 webhook 请求头，可重复")
    p.add_argument("--token", default=_env("SENTINEL_TOKEN"),
                   help="中间层 API_TOKEN，带上后可顺便校验鉴权是否正常")
    p.add_argument("--interval", type=int, default=int(_env("SENTINEL_INTERVAL", "60")),
                   help="轮询间隔秒，默认 60")
    p.add_argument("--retries", type=int, default=int(_env("SENTINEL_RETRIES", "2")),
                   help="连续失败多少次才判定故障，默认 2")
    p.add_argument("--timeout", type=float, default=float(_env("SENTINEL_TIMEOUT", "8")),
                   help="单次请求超时秒，默认 8")
    p.add_argument("--state-file", default=_env("SENTINEL_STATE_FILE", STATE_PATH),
                   help="状态文件，用于跨次运行保持「当前是否已告警」")
    p.add_argument("--once", action="store_true", help="只检查一轮就退出（配合 cron）")
    p.add_argument("--quiet", action="store_true", help="不打印正常心跳")
    p.add_argument("--version", action="version", version=f"sentinel {VERSION}")
    return p.parse_args(argv)


# ---------------------------------------------------------------- 探测

def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def check_health(url: str, token: str, timeout: float) -> tuple[bool, str, dict]:
    """返回 (是否健康, 说明, 原始 data)。

    给了 token 时会额外打一个需要鉴权的接口，确认中间层不只是活着、
    而是真的能代替 App 干活（token 被改坏时能第一时间发现）。
    """
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", {}
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return False, f"连接失败：{exc}", {}

    if status != 200:
        return False, f"HTTP {status}", {}
    try:
        payload = json.loads(body)
    except ValueError:
        return False, "响应不是 JSON", {}

    data = payload.get("data") or {}
    ugos = data.get("ugos") or {}
    if not payload.get("ok"):
        return False, "服务自报 not ok", data
    # 中间层活着但连不上 NAS，也算故障（这才是用户真正关心的）
    if ugos and ugos.get("reachable") is False:
        return False, "中间层在线但 NAS 不可达", data
    if ugos and ugos.get("authenticated") is False:
        return False, "中间层在线但 UGOS 会话未认证", data

    if token:
        probe = url.split("?")[0].rstrip("/")
        if probe.endswith("/health"):
            probe = probe[: -len("/health")] + "/api/v1/capabilities"
        auth_req = urllib.request.Request(probe, headers={"Accept": "application/json"})
        auth_req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(auth_req, timeout=timeout, context=_ctx()) as resp:
                if resp.status == 401:
                    return False, "中间层鉴权失败（API_TOKEN 可能已更换）", data
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, "中间层鉴权失败（API_TOKEN 可能已更换）", data
            return False, f"鉴权探测 HTTP {exc.code}", data
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            return False, f"鉴权探测失败：{exc}", data

    return True, "ok", data



def check_port(target: str, timeout: float) -> tuple[bool, str]:
    """TCP + TLS 握手，确认 NAS 上那个 HTTPS 服务还在。"""
    host, _, port_s = target.partition(":")
    if not host or not port_s.isdigit():
        return False, f"目标格式错误：{target}（应为 host:port）"
    port = int(port_s)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with _ctx().wrap_socket(sock, server_hostname=host):
                return True, "ok"
    except (socket.timeout, OSError, ssl.SSLError) as exc:
        return False, f"TLS 握手失败：{exc}"


# ---------------------------------------------------------------- webhook

def send_webhook(url: str, fmt: str, headers: list[str], payload: dict, timeout: float) -> bool:
    if not url:
        return False
    text = payload.get("text", "")
    if fmt == "text":
        body = text.encode("utf-8")
        ctype = "text/plain; charset=utf-8"
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ctype = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", ctype)
    for item in headers:
        key, _, val = item.partition(":")
        if key.strip():
            req.add_header(key.strip(), val.strip())
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        print(f"[sentinel] webhook 返回 HTTP {exc.code}", file=sys.stderr)
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        print(f"[sentinel] webhook 发送失败：{exc}", file=sys.stderr)
    return False


# ---------------------------------------------------------------- 状态

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(state, fp)
    except OSError as exc:
        print(f"[sentinel] 状态文件写入失败：{exc}", file=sys.stderr)


# ---------------------------------------------------------------- 主循环

def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def run_once(args: argparse.Namespace, state: dict) -> dict:
    """检查一轮，必要时发 webhook，返回更新后的 state。"""
    now = int(time.time())
    failures: list[str] = []

    if args.url:
        ok, detail, data = check_health(args.url, args.token, args.timeout)
        if ok:
            state["health_fail_streak"] = 0
            state["last_detail"] = detail
        else:
            state["health_fail_streak"] = int(state.get("health_fail_streak", 0)) + 1
            state["last_detail"] = detail
            if state["health_fail_streak"] >= args.retries:
                failures.append(f"中间层 {args.url} 异常：{detail}")

    if args.nas:
        ok, detail = check_port(args.nas, args.timeout)
        if ok:
            state["nas_fail_streak"] = 0
        else:
            state["nas_fail_streak"] = int(state.get("nas_fail_streak", 0)) + 1
            state["nas_detail"] = detail
            if state["nas_fail_streak"] >= args.retries:
                failures.append(f"NAS {args.nas} 不可达：{detail}")

    broken = bool(failures)
    was_broken = bool(state.get("alerting"))

    if broken and not was_broken:
        text = "【NAS 告警】" + "；".join(failures)
        if send_webhook(args.webhook, args.webhook_format, args.webhook_header,
                        {"text": text, "title": "NAS 告警", "level": "critical",
                         "source": "sentinel", "ts": now, "targets": failures}, args.timeout):
            text += "（已推送）"
        print(text, flush=True)
        state["alerting"] = True
        state["alerted_at"] = now
    elif broken and was_broken:
        log(f"仍异常（未重复推送）：{'；'.join(failures)}", args.quiet)
    elif not broken and was_broken:
        text = "【NAS 恢复】哨兵检查恢复正常"
        state["alerting"] = False
        state["recovered_at"] = now
        if send_webhook(args.webhook, args.webhook_format, args.webhook_header,
                        {"text": text, "title": "NAS 恢复", "level": "info",
                         "source": "sentinel", "ts": now}, args.timeout):
            text += "（已推送）"
        print(text, flush=True)
    else:
        log(f"正常 {state.get('last_detail', 'ok')}", args.quiet)

    state["checked_at"] = now
    return state


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.url and not args.nas:
        print("[sentinel] 至少要给 --url 或 --nas 其中之一", file=sys.stderr)
        return 2

    state = load_state(args.state_file)
    while True:
        try:
            state = run_once(args, state)
            save_state(args.state_file, state)
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # noqa: BLE001 - 哨兵自身不能因异常退出
            print(f"[sentinel] 本轮检查异常：{exc}", file=sys.stderr)

        if args.once:
            break
        try:
            time.sleep(max(5, args.interval))
        except KeyboardInterrupt:
            return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
