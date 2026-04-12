from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NovaError:
    """A single ERROR-level log entry emitted by a Nova service."""
    timestamp: str          # ISO timestamp from structlog
    event: str              # structlog "event" field  e.g. "ha_proxy.camera_error"
    exc_type: str           # exception class name     e.g. "RemoteProtocolError"
    exc_value: str          # str(exc) — may be empty
    logger: str             # structlog "logger"       e.g. "avatar_backend.services.ha_proxy"
    service: str            # systemd unit name        e.g. "avatar-backend"
    level: str              # "error" / "warning"
    raw_json: str           # original structlog JSON line (for context)

    @property
    def dedup_key(self) -> str:
        """Stable key used for deduplication: event + exc_type."""
        return f"{self.event}|{self.exc_type}"

    @property
    def short_summary(self) -> str:
        parts = [self.event]
        if self.exc_type:
            parts.append(f"{self.exc_type}: {self.exc_value}" if self.exc_value else self.exc_type)
        return " — ".join(parts)


@dataclass
class PendingFix:
    """A proposed fix awaiting Telegram approval."""
    fix_id: str             # UUID, used as callback_data key
    error: NovaError
    source_file: str        # absolute path to the .py file
    diff: str               # unified diff text (may be empty if Claude produced analysis only)
    summary: str            # one-paragraph plain-English summary from Claude
    created_at: float       # time.monotonic() for timeout tracking
    telegram_message_id: Optional[int] = None
    has_diff: bool = field(init=False)

    def __post_init__(self) -> None:
        self.has_diff = bool(self.diff and self.diff.strip())
