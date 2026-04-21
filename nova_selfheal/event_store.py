from __future__ import annotations
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import structlog

_LOGGER = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS heal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,           -- monotonic time
    ts_iso      TEXT    NOT NULL,           -- ISO-8601 wall-clock
    event_type  TEXT    NOT NULL,           -- see EVENT_* constants below
    fix_id      TEXT    NOT NULL DEFAULT '',
    service     TEXT    NOT NULL DEFAULT '',
    log_event   TEXT    NOT NULL DEFAULT '',
    exc_type    TEXT    NOT NULL DEFAULT '',
    source_file TEXT    NOT NULL DEFAULT '',
    summary     TEXT    NOT NULL DEFAULT '',
    diff        TEXT    NOT NULL DEFAULT '',
    message     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_heal_ts ON heal_events(ts);
CREATE INDEX IF NOT EXISTS idx_heal_type ON heal_events(event_type);
"""

# event_type constants
EVT_ERROR_DETECTED  = "error_detected"
EVT_DEDUPLICATED    = "deduplicated"
EVT_NO_SOURCE       = "no_source_file"
EVT_CLAUDE_FAILED   = "claude_failed"
EVT_FIX_PROPOSED    = "fix_proposed"
EVT_ANALYSIS_ONLY   = "analysis_only"
EVT_APPROVED        = "approved"
EVT_REJECTED        = "rejected"
EVT_AUTO_REJECTED   = "auto_rejected"
EVT_APPLY_OK        = "apply_ok"
EVT_APPLY_FAILED    = "apply_failed"
EVT_RESTART_OK      = "restart_ok"
EVT_RESTART_FAILED  = "restart_failed"
EVT_AUTO_RESTART    = "auto_restart"          # health-checker initiated restart
EVT_HEALTH_DOWN     = "health_check_failed"   # /health confirmed unreachable
EVT_VERIFY_OK       = "verify_ok"
EVT_VERIFY_FAILED   = "verify_failed"


class EventStore:
    """Append-only log of all self-heal pipeline events."""

    def __init__(self, db_path: Path) -> None:
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)

    def record(self, event_type: str, **kwargs: Any) -> None:
        from datetime import datetime, timezone
        ts = time.monotonic()
        ts_iso = datetime.now(timezone.utc).isoformat()
        row = {
            "ts": ts,
            "ts_iso": ts_iso,
            "event_type": event_type,
            "fix_id":      kwargs.get("fix_id", ""),
            "service":     kwargs.get("service", ""),
            "log_event":   kwargs.get("log_event", ""),
            "exc_type":    kwargs.get("exc_type", ""),
            "source_file": kwargs.get("source_file", ""),
            "summary":     kwargs.get("summary", ""),
            "diff":        kwargs.get("diff", ""),
            "message":     kwargs.get("message", ""),
        }
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    """INSERT INTO heal_events
                       (ts, ts_iso, event_type, fix_id, service, log_event,
                        exc_type, source_file, summary, diff, message)
                       VALUES (:ts,:ts_iso,:event_type,:fix_id,:service,:log_event,
                               :exc_type,:source_file,:summary,:diff,:message)""",
                    row,
                )
        except Exception as exc:
            _LOGGER.warning("event_store.write_failed", exc=repr(exc))

    def get_recent(self, limit: int = 100) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM heal_events ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        with self._lock, self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM heal_events").fetchone()[0]
            errors = conn.execute(
                "SELECT COUNT(*) FROM heal_events WHERE event_type=?",
                (EVT_ERROR_DETECTED,),
            ).fetchone()[0]
            applied = conn.execute(
                "SELECT COUNT(*) FROM heal_events WHERE event_type=?",
                (EVT_APPLY_OK,),
            ).fetchone()[0]
            rejected = conn.execute(
                "SELECT COUNT(*) FROM heal_events WHERE event_type IN (?,?)",
                (EVT_REJECTED, EVT_AUTO_REJECTED),
            ).fetchone()[0]
        return {
            "total_events": total,
            "errors_detected": errors,
            "patches_applied": applied,
            "fixes_rejected": rejected,
        }

    def clear_all(self) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM heal_events")
            return cur.rowcount
