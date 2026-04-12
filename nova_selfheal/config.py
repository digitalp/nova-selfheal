from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token: str
    telegram_chat_id: int

    # ── Nova paths ────────────────────────────────────────────────────────────
    nova_path: Path = Path("/opt/avatar-server")
    watch_service: str = "avatar-backend"
    claude_work_dir: Path = Path("/opt/avatar-server")

    # ── Timing ────────────────────────────────────────────────────────────────
    # How long (seconds) to wait for user approval before auto-rejecting
    approval_timeout_seconds: int = 1800   # 30 minutes
    # Suppress duplicate (event+exc_type) within this window
    dedup_window_seconds: int = 1800       # 30 minutes
    # Hard timeout for Claude CLI subprocess
    claude_timeout_seconds: int = 120

    # ── Storage ───────────────────────────────────────────────────────────────
    db_path: Path = Path("/opt/nova-selfheal/dedup.db")

    # ── API server ────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 7779

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file="/opt/nova-selfheal/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
