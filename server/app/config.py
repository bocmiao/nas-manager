"""中间层配置。所有敏感值从环境变量 / .env 读取，不写死。"""
from __future__ import annotations

import secrets
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- UGOS 目标机（192.0.2.10 是 RFC 5737 文档占位地址，须由 .env 覆盖）---
    ugos_host: str = "192.0.2.10"
    ugos_port: int = 9443
    ugos_user: str = ""
    ugos_password: str = ""
    ugos_timeout: float = 20.0

    # --- 中间层自身 ---
    api_token: str = ""
    server_host: str = "0.0.0.0"
    server_port: int = 8787
    log_level: str = "INFO"

    # --- 缓存 TTL（秒）---
    ttl_overview: float = 2.0
    ttl_system: float = 300.0
    ttl_storage: float = 30.0
    ttl_docker: float = 5.0
    ttl_logs: float = 10.0

    # --- 巡检与告警阈值 ---
    poll_interval: int = 60
    alert_disk_temp: float = 50.0
    alert_cpu_temp: float = 80.0
    alert_volume_used: float = 90.0
    alert_cpu_used: float = 95.0
    alert_mem_used: float = 92.0
    alert_fan_stall_seconds: int = 300

    # --- 限流 ---
    rate_limit_per_min: int = 240

    @property
    def ugos_base(self) -> str:
        return f"https://{self.ugos_host}:{self.ugos_port}"

    def resolve_api_token(self) -> str:
        """未配置时生成临时 token（重启失效），仅用于首次联调。"""
        if not self.api_token:
            self.api_token = secrets.token_urlsafe(24)
        return self.api_token


@lru_cache
def get_settings() -> Settings:
    return Settings()
