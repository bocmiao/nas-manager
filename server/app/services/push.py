"""APNs 推送通道。

- 设备注册表存在内存里（进程重启即清空，App 每次启动重新注册即可）
- 推送用 httpx 直连 `api.push.apple.com`，HTTP/2 + ES256 JWT 鉴权
- 凭据未配置 / 缺 h2 依赖 / 网络不通时**只写日志**，绝不抛异常影响巡检
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field

from ..config import Settings
from .alerts import AlertEvent

log = logging.getLogger("push")

JWT_TTL = 3000  # Apple 要求 provider token 20~60 分钟刷新一次，取 50 分钟


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass
class Device:
    token: str
    platform: str = "ios"
    name: str = ""
    registered_at: float = field(default_factory=time.time)
    last_ok_at: float | None = None
    last_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "token": self.token[:8] + "…" if len(self.token) > 8 else self.token,
            "token_length": len(self.token),
            "platform": self.platform,
            "name": self.name,
            "registered_at": int(self.registered_at),
            "last_ok_at": int(self.last_ok_at) if self.last_ok_at else None,
            "last_error": self.last_error,
        }


class PushService:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._devices: dict[str, Device] = {}
        self._jwt: str = ""
        self._jwt_at: float = 0.0
        self._dedup: dict[str, float] = {}
        self._client = None
        self._init_error: str | None = None
        self._sent = 0
        self._failed = 0

    # ---------------------------------------------------------------- 设备

    def register(self, token: str, platform: str = "ios", name: str = "") -> dict:
        token = (token or "").strip().replace(" ", "").lower()
        if len(token) < 32:
            raise ValueError("设备 token 长度不足，需 64 位十六进制")
        dev = self._devices.get(token) or Device(token=token)
        dev.platform = platform or "ios"
        dev.name = name or dev.name
        if token not in self._devices:
            dev.registered_at = time.time()
        self._devices[token] = dev
        log.info("注册推送设备 %s…（%s）", token[:8], dev.platform)
        return dev.to_dict()

    def unregister(self, token: str) -> bool:
        return self._devices.pop((token or "").strip().lower(), None) is not None

    def devices(self) -> list[dict]:
        return [d.to_dict() for d in self._devices.values()]

    # ---------------------------------------------------------------- JWT

    def _provider_jwt(self) -> str:
        """ES256 JWT：header.claims.signature，签名需转成 r||s 定长 64 字节。"""
        if self._jwt and time.time() - self._jwt_at < JWT_TTL:
            return self._jwt

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        with open(self._s.apns_key_file, "rb") as fp:
            key = serialization.load_pem_private_key(fp.read(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("APNs 私钥必须是 ES256（P-256）椭圆曲线密钥")

        header = _b64u(json.dumps({"alg": "ES256", "kid": self._s.apns_key_id}).encode())
        claims = _b64u(
            json.dumps({"iss": self._s.apns_team_id, "iat": int(time.time())}).encode()
        )
        signing_input = f"{header}.{claims}".encode()
        der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

        self._jwt = f"{header}.{claims}.{sig}"
        self._jwt_at = time.time()
        return self._jwt

    # ---------------------------------------------------------------- 发送

    async def _ensure_client(self):
        if self._client is not None or self._init_error:
            return self._client
        try:
            import httpx

            self._client = httpx.AsyncClient(http2=True, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - 缺 h2 时 httpx 会在这里报错
            self._init_error = f"HTTP/2 初始化失败（需要 h2 包）：{exc}"
            log.warning("APNs 不可用：%s", self._init_error)
        return self._client

    async def send_alert(self, event: AlertEvent) -> int:
        """推送一条告警事件，返回成功设备数。任何异常都吞掉只记日志。"""
        alert = event.alert
        if event.action == "resolved":
            title = f"已恢复：{alert.title}"
            body = f"持续 {int(time.time() - alert.since) // 60} 分钟，现已恢复。"
        else:
            title = alert.title
            body = alert.detail

        payload = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": "default",
                "interruption-level": "time-sensitive" if alert.level == "critical" else "active",
                "thread-id": alert.scope,
            },
            "alert_key": alert.key,
            "level": alert.level,
            "scope": alert.scope,
            "action": event.action,
        }
        return await self.push(payload, dedup_key=f"{alert.key}:{event.action}")

    async def push(self, payload: dict, dedup_key: str = "") -> int:
        if not self._s.apns_configured:
            log.info("[推送降级] 未配置 APNs 凭据，仅记录：%s", payload.get("aps", {}).get("alert"))
            return 0
        if not self._devices:
            log.debug("无已注册设备，跳过推送")
            return 0

        # 同一告警 key 的去重窗口，避免服务重启后重复轰炸
        if dedup_key:
            now = time.time()
            last = self._dedup.get(dedup_key)
            if last and now - last < self._s.apns_push_dedup_seconds:
                log.debug("推送去重命中：%s", dedup_key)
                return 0
            self._dedup[dedup_key] = now
            for k in [k for k, t in self._dedup.items() if now - t > 86400]:
                self._dedup.pop(k, None)

        client = await self._ensure_client()
        if client is None:
            return 0

        try:
            jwt = self._provider_jwt()
        except Exception as exc:  # noqa: BLE001
            log.warning("APNs JWT 生成失败：%s", exc)
            return 0

        ok = 0
        for dev in list(self._devices.values()):
            url = f"{self._s.apns_host}/3/device/{dev.token}"
            headers = {
                "authorization": f"bearer {jwt}",
                "apns-topic": self._s.apns_topic,
                "apns-push-type": "alert",
                "apns-priority": "10" if payload.get("level") == "critical" else "5",
                "apns-expiration": str(int(time.time()) + 3600),
            }
            try:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code == 200:
                    dev.last_ok_at = time.time()
                    dev.last_error = None
                    ok += 1
                    self._sent += 1
                else:
                    reason = ""
                    try:
                        reason = (r.json() or {}).get("reason", "")
                    except Exception:  # noqa: BLE001
                        reason = r.text[:120]
                    dev.last_error = f"HTTP {r.status_code} {reason}".strip()
                    self._failed += 1
                    log.warning("APNs 推送失败 %s…：%s", dev.token[:8], dev.last_error)
                    # 410 Unregistered 说明设备已卸载，直接清掉
                    if r.status_code == 410 or reason == "BadDeviceToken":
                        self._devices.pop(dev.token, None)
            except Exception as exc:  # noqa: BLE001 - 网络问题不影响巡检
                dev.last_error = str(exc)
                self._failed += 1
                log.warning("APNs 连接异常：%s", exc)
        return ok

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def stats(self) -> dict:
        return {
            "configured": self._s.apns_configured,
            "host": self._s.apns_host if self._s.apns_configured else None,
            "topic": self._s.apns_topic or None,
            "devices": len(self._devices),
            "sent": self._sent,
            "failed": self._failed,
            "init_error": self._init_error,
        }
