"""UGOS 私有接口客户端。

实测契约要点（详见 docs/api-verified.md）：
- 公钥在响应头 X-Rsa-Token，base64 编码的 PKCS#1 PEM
- 密码用 RSA PKCS#1 v1.5 加密，超长分段后 base64 拼接
- 读操作主通道：GET + ?token=<明文 token>
- POST 带 token 多数接口返回 9404，因此 token 一律走 query
- 会话过期返回 1024 / 1010，需静默重登
- 写操作（磁盘测速等）需额外请求头 `X-CurPass-Token`，值为 RSA 加密后的密码
"""
from __future__ import annotations

import asyncio
import base64
import logging

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger("ugos")

# 需要重新登录的错误码
AUTH_FAIL_CODES = frozenset({401, 1000, 1001, 1002, 1003, 1010, 1013, 1024, 1204})

DEFAULT_HEADERS = {
    "UG-Agent": "PC/WEB",
    "Client-Version": "78471",
    "Client-Version-Str": "1.19.0",
    "X-Specify-Language": "zh-CN",
    "Cache-Control": "no-cache",
    "Accept": "application/json",
}


class UgosError(Exception):
    """UGOS 业务错误。code 为接口返回码，非 HTTP 状态码。

    `debug` 是接口附带的细节（例如 9999 时的 `disk is busy: 1`），
    比笼统的 `msg` 有用得多，单独保留。
    """

    def __init__(self, code: int, msg: str = "", path: str = "", debug: str = "") -> None:
        self.code = code
        self.msg = msg or f"UGOS error {code}"
        self.path = path
        self.debug = debug
        super().__init__(f"[{code}] {self.msg}" + (f" ({path})" if path else ""))


class UgosAuthRequired(UgosError):
    """登录被拒（账号密码错误或需要二次验证）。"""


class UgosClient:
    def __init__(self, base: str, username: str, password: str, timeout: float = 20.0) -> None:
        self._base = base.rstrip("/")
        self._username = username
        self._password = password
        self._token = ""
        self._pass_token = ""
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=self._base,
            verify=False,
            timeout=timeout,
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
        )

    # ---------- 生命周期 ----------

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def has_session(self) -> bool:
        return bool(self._token)

    # ---------- 认证 ----------

    @staticmethod
    def _encrypt_password(pem: bytes, plain: str) -> str:
        pub = serialization.load_pem_public_key(pem)
        raw = plain.encode("utf-8")
        chunk = 245  # RSA-2048 + PKCS#1 v1.5 单块上限
        parts = [
            base64.b64encode(pub.encrypt(raw[i : i + chunk], padding.PKCS1v15())).decode()
            for i in range(0, len(raw), chunk)
        ]
        return "".join(parts)

    async def login(self) -> str:
        """完整登录，返回 token。失败抛 UgosAuthRequired。"""
        if not self._username or not self._password:
            raise UgosAuthRequired(-1, "未配置 UGOS_USER / UGOS_PASSWORD")

        try:
            r = await self._http.post(
                "/ugreen/v1/verify/check", json={"username": self._username}
            )
        except httpx.HTTPError as exc:
            raise UgosError(-1, f"无法连接 NAS：{exc}") from exc

        pub_b64 = r.headers.get("x-rsa-token")
        if not pub_b64:
            raise UgosAuthRequired(-1, "verify/check 未返回 X-Rsa-Token 响应头")

        try:
            pem = base64.b64decode(pub_b64)
        except ValueError as exc:
            raise UgosAuthRequired(-1, "X-Rsa-Token 不是合法 base64") from exc

        payload = {
            "username": self._username,
            "password": self._encrypt_password(pem, self._password),
            "keepalive": False,
            "otp": True,
            "is_simple": False,
        }
        r = await self._http.post("/ugreen/v1/verify/login", json=payload)
        try:
            data = r.json()
        except ValueError as exc:
            raise UgosAuthRequired(-1, f"登录响应不是 JSON（HTTP {r.status_code}）") from exc

        if data.get("code") != 200:
            raise UgosAuthRequired(data.get("code") or -1, data.get("msg") or "登录失败")

        body = data.get("data") or {}
        token = body.get("token") or ""
        if not token:
            if body.get("enable_otp"):
                raise UgosAuthRequired(-1, "该账号开启了二次验证，需先关闭或改用应用专用密码")
            raise UgosAuthRequired(-1, "登录成功但未返回 token")

        self._token = token
        log.info("UGOS 登录成功 user=%s token=%s…", self._username, token[:6])
        return token

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        async with self._lock:
            if not self._token:
                await self.login()
            return self._token

    async def _relogin(self) -> None:
        async with self._lock:
            self._token = ""
            await self.login()

    async def password_token(self, *, refresh: bool = False) -> str:
        """取「本次操作密码令牌」。

        实测：磁盘测速、删池等写操作需要在请求头带 `X-CurPass-Token`，
        值就是 RSA 加密后的账号密码，缺失时返回 code 1101「密码验证失败」。
        """
        if self._pass_token and not refresh:
            return self._pass_token
        r = await self._http.post("/ugreen/v1/verify/check", json={"username": self._username})
        pub = r.headers.get("x-rsa-token")
        if not pub:
            raise UgosAuthRequired(-1, "verify/check 未返回 X-Rsa-Token，无法生成密码令牌")
        self._pass_token = self._encrypt_password(base64.b64decode(pub), self._password)
        return self._pass_token

    # ---------- 请求 ----------

    def _url(self, path: str) -> str:
        p = path.lstrip("/")
        if not p.startswith("ugreen/"):
            p = "ugreen/" + p
        return "/" + p

    async def get(self, path: str, params: dict | None = None, *, retry: bool = True,
                  headers: dict | None = None):
        token = await self._ensure_token()
        q = dict(params or {})
        q["token"] = token
        r = await self._http.get(self._url(path), params=q, headers=headers)
        data = self._parse(r, path)

        code = data.get("code")
        if code == 200:
            return data.get("data")
        if code in AUTH_FAIL_CODES and retry:
            log.warning("会话失效 code=%s，重新登录后重试 %s", code, path)
            await self._relogin()
            return await self.get(path, params, retry=False, headers=headers)
        raise UgosError(code, data.get("msg") or "", path, data.get("debug") or "")

    async def post(self, path: str, body: dict | None = None, params: dict | None = None,
                   *, retry: bool = True, headers: dict | None = None):
        """POST 的 token 同样走 query（实测 header 方式多返回 9404）。"""
        token = await self._ensure_token()
        q = dict(params or {})
        q["token"] = token
        r = await self._http.post(self._url(path), json=body or {}, params=q, headers=headers)
        data = self._parse(r, path)

        code = data.get("code")
        if code == 200:
            return data.get("data")
        if code in AUTH_FAIL_CODES and retry:
            log.warning("会话失效 code=%s，重新登录后重试 %s", code, path)
            await self._relogin()
            return await self.post(path, body, params, retry=False, headers=headers)
        raise UgosError(code, data.get("msg") or "", path, data.get("debug") or "")

    async def post_privileged(self, path: str, body: dict | None = None):
        """需要二级密码确认的写操作：自动附带 X-CurPass-Token。"""
        tok = await self.password_token()
        try:
            return await self.post(path, body, headers={"X-CurPass-Token": tok})
        except UgosError as exc:
            # 令牌过期则刷新后重试一次
            if exc.code == 1101:
                tok = await self.password_token(refresh=True)
                return await self.post(path, body, headers={"X-CurPass-Token": tok})
            raise

    @staticmethod
    def _parse(r: httpx.Response, path: str) -> dict:
        try:
            data = r.json()
        except ValueError:
            raise UgosError(-1, f"HTTP {r.status_code} 非 JSON 响应（路径可能不存在）", path) from None
        if not isinstance(data, dict):
            raise UgosError(-1, "响应结构异常", path)
        return data
