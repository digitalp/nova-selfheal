from __future__ import annotations
import hashlib
import sqlite3
import time
from pathlib import Path

from nova_selfheal.models import NovaError


class ErrorDeduplicator:
    """
    SQLite-backed deduplication. Suppresses the same (event, exc_type) pair
    if it has already been seen within `window_seconds`.
    Survives process restarts.
    """

    _CREATE = """
        CREATE TABLE IF NOT EXISTS seen_errors (
            key       TEXT PRIMARY KEY,
            first_seen REAL NOT NULL,
            count      INTEGER NOT NULL DEFAULT 1
        )
    """

    def __init__(self, db_path: Path, window_seconds: int) -> None:
        self._db_path = db_path
        self._window = window_seconds
        self._conn: sqlite3.Connection | None = None

    def setup(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute(self._CREATE)
        self._conn.commit()

    def _key(self, error: NovaError) -> str:
        raw = f"{error.event}|{error.exc_type}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _cleanup(self) -> None:
        cutoff = time.time() - self._window
        assert self._conn
        self._conn.execute("DELETE FROM seen_errors WHERE first_seen < ?", (cutoff,))
        self._conn.commit()

    def is_duplicate(self, error: NovaError) -> bool:
        assert self._conn, "call setup() first"
        self._cleanup()
        key = self._key(error)
        row = self._conn.execute(
            "SELECT first_seen FROM seen_errors WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return False
        # Row exists and was not cleaned up → still within window
        return True

    def record(self, error: NovaError) -> None:
        assert self._conn, "call setup() first"
        key = self._key(error)
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO seen_errors (key, first_seen, count) VALUES (?, ?, 1)
            ON CONFLICT(key) DO UPDATE SET count = count + 1
            """,
            (key, now),
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
