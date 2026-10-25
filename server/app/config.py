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
    ttl_smart: float = 60.0

    # --- 巡检与告警阈值 ---
    poll_interval: int = 60
    alert_disk_temp: float = 50.0
    alert_disk_temp_critical: float = 58.0
    alert_cpu_temp: float = 80.0
    alert_volume_used: float = 90.0
    alert_volume_used_critical: float = 97.0
    alert_cpu_used: float = 95.0
    alert_mem_used: float = 92.0
    alert_fan_stall_seconds: int = 300

    # --- 新增告警阈值 ---
    # SMART 累计计数超过该值即告警（0 表示关闭该条）
    alert_smart_reallocated: int = 1
    alert_smart_pending: int = 1
    alert_smart_offline_uncorrectable: int = 1
    # 容器在窗口内重启次数达到阈值 → 判定重启循环
    alert_container_restart_window: int = 600
    alert_container_restart_count: int = 3
    # 风扇转速低于该值且风扇处于启用状态即告警
    alert_fan_min_rpm: int = 100
    # 网口协商速率低于该值告警；0 = 只检测「相对历史峰值降速」
    alert_nic_min_mbps: int = 0

    # --- 磁盘测速 ---
    benchmark_poll_seconds: float = 3.0
    benchmark_timeout_seconds: int = 1800
    # 测速属写盘操作，默认关闭；开启后接口才允许真正发起测试
    benchmark_allow_start: bool = True

    # --- APNs 推送 ---
    # 全部留空时推送通道降级为「只写日志」，不影响服务启动
    apns_key_file: str = ""          # .p8 私钥路径
    apns_key_id: str = ""            # Apple 后台的 Key ID
    apns_team_id: str = ""           # 开发者 Team ID
    apns_topic: str = ""             # App 的 Bundle ID
    apns_use_sandbox: bool = False   # True 走 api.sandbox.push.apple.com
    apns_push_dedup_seconds: int = 600

    # --- 限流 ---
    rate_limit_per_min: int = 240

    @property
    def ugos_base(self) -> str:
        return f"https://{self.ugos_host}:{self.ugos_port}"

    @property
    def apns_configured(self) -> bool:
        """四项齐全才认为推送通道可用。"""
        return bool(self.apns_key_file and self.apns_key_id and self.apns_team_id and self.apns_topic)

    @property
    def apns_host(self) -> str:
        return "https://api.sandbox.push.apple.com" if self.apns_use_sandbox else "https://api.push.apple.com"

    def resolve_api_token(self) -> str:
        """未配置时生成临时 token（重启失效），仅用于首次联调。"""
        if not self.api_token:
            self.api_token = secrets.token_urlsafe(24)
        return self.api_token


@lru_cache
def get_settings() -> Settings:
    return Settings()
