#!/usr/bin/env python3
"""UGOS Pro 登录链路验证。

用法:
    python scripts/nas_login_test.py                # 从 .env 读凭据
    python scripts/nas_login_test.py <user> <pass>  # 命令行传参

验证内容:
    1. POST /ugreen/v1/verify/check 拿 X-Rsa-Token 公钥
    2. RSA PKCS1v1.5 加密密码
    3. POST /ugreen/v1/verify/login 换取 token
    4. 带 token 请求一个只读接口确认会话可用
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_VERSION = "78471"
CLIENT_VERSION_STR = "1.19.0"

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class UgosClient:
    def __init__(self, host: str, port: int = 9443):
        self.base = f"https://{host}:{port}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ssl_ctx),
        )
        self.token = ""
        self.rsa_pub_pem = b""

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "UG-Agent": "PC/WEB",
            "Client-Version": CLIENT_VERSION,
            "Client-Version-Str": CLIENT_VERSION_STR,
            "X-Specify-Language": "zh-CN",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    def _url(self, path: str) -> str:
        p = path.lstrip("/")
        if not p.startswith("ugreen/"):
            p = "ugreen/" + p
        return f"{self.base}/{p}"

    def post(self, path: str, payload: dict | None, headers: dict[str, str] | None = None):
        url = self._url(path)
        body = json.dumps(payload or {}).encode()
        req = urllib.request.Request(url, data=body, headers=self._headers(headers), method="POST")
        try:
            with self.opener.open(req, timeout=15) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), {"error": e.read().decode("utf-8", "ignore")[:300]}

    def get(self, path: str, params: dict | None = None, headers: dict[str, str] | None = None):
        url = self._url(path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(headers), method="GET")
        try:
            with self.opener.open(req, timeout=15) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), {"error": e.read().decode("utf-8", "ignore")[:300]}

    def fetch_rsa_key(self, username: str) -> bytes:
        st, hdrs, data = self.post("ugreen/v1/verify/check", {"username": username})
        token = None
        for k, v in hdrs.items():
            if k.lower() == "x-rsa-token":
                token = v
                break
        if not token:
            raise RuntimeError(f"no X-Rsa-Token header. status={st} body={data}")
        return base64.b64decode(token)

    @staticmethod
    def encrypt_password(pem: bytes, password: str) -> str:
        pub = serialization.load_pem_public_key(pem)
        raw = password.encode()
        chunk = 245  # RSA-2048 + PKCS1v1.5 单块上限
        parts = [pub.encrypt(raw[i:i + chunk], padding.PKCS1v15()) for i in range(0, len(raw), chunk)]
        return "".join(base64.b64encode(p).decode() for p in parts)

    def login(self, username: str, password: str) -> dict:
        self.rsa_pub_pem = self.fetch_rsa_key(username)
        enc = self.encrypt_password(self.rsa_pub_pem, password)
        payload = {
            "username": username,
            "password": enc,
            "keepalive": False,
            "otp": True,
            "is_simple": False,
        }
        st, hdrs, data = self.post("ugreen/v1/verify/login", payload)
        body = data.get("data") or {}
        self.token = body.get("token") or ""
        return {"status": st, "code": data.get("code"), "msg": data.get("msg"),
                "enable_otp": body.get("enable_otp"), "has_token": bool(self.token),
                "cookies": [c.name for c in self.jar]}


def main() -> int:
    env = load_env()
    if len(sys.argv) >= 3:
        host, user, pwd = env.get("UGOS_HOST", "192.0.2.10"), sys.argv[1], sys.argv[2]
    else:
        host = env.get("UGOS_HOST", "192.0.2.10")
        user = env.get("UGOS_USER", "")
        pwd = env.get("UGOS_PASSWORD", "")
    port = int(env.get("UGOS_PORT", "9443"))

    if not user or not pwd:
        print("缺少凭据：请在项目根目录创建 .env（参考 .env.example），或命令行传参 user pass")
        return 2

    print(f"target: https://{host}:{port}  user={user}")
    c = UgosClient(host, port)
    pem = c.fetch_rsa_key(user)
    print(f"[1] rsa key ok, {len(pem)} bytes, head={pem[:32]!r}")

    info = c.login(user, pwd)
    print(f"[2] login -> {info}")

    if not info["has_token"]:
        print("登录未拿到 token，可能是账号密码错误或开启了二次验证")
        return 1

    # token 默认走 URL query（token_where == "url"），同时带 md5(token) 作为 security key
    import hashlib
    sec = hashlib.md5(c.token.encode()).hexdigest()
    probes = [
        ("v1/user/current/user", {}),
        ("v1/verify/is_login", {}),
    ]
    for path, params in probes:
        p = dict(params)
        p["token"] = c.token
        st, _, data = c.get(path, p, {"X-Ugreen-Security-Key": sec})
        print(f"[3] GET {path} -> status={st} code={data.get('code')} msg={data.get('msg')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
