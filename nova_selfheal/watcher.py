from __future__ import annotations
import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

import structlog

from nova_selfheal.known_fixes import match_known_error
from nova_selfheal.models import NovaError

if TYPE_CHECKING:
    from nova_selfheal.config import Settings

_LOGGER = structlog.get_logger(__name__)

# journald PRIORITY values: 0=EMERG 1=ALERT 2=CRIT 3=ERR 4=WARNING 5=NOTICE 6=INFO
# Uvicorn writes everything to stdout so journald tags all lines as PRIORITY=6.
# We rely on the inner structlog `level` field instead — accept up to INFO (6)
# and let the inner JSON filter decide what's an error.
_MAX_PRIORITY = 6


class JournalWatcher:
    """
    Tails the systemd journal for a given service unit in JSON output mode.
    Parses each line as outer journald JSON, then inner structlog JSON,
    and forwards ERROR-level entries as NovaError onto an asyncio.Queue.
    """

    def __init__(self, service: str, queue: asyncio.Queue, settings: "Settings") -> None:
        self._service = service
        self._queue = queue
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._traceback_lines: list[str] = []

    async def start(self) -> None:
        self._task = asyncio.create_task(self._watch_loop(), name="journal_watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass

    async def _watch_loop(self) -> None:
        backoff = 2
        while True:
            try:
                await self._run()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                _LOGGER.warning("watcher.restart", exc=repr(exc), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                # journalctl exited cleanly — restart to re-attach
                await asyncio.sleep(2)

    async def _run(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            "journalctl",
            "-u", self._service,
            "-f",
            "--output=json",
            "--lines=0",          # only new lines from now on
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            error = self._parse(line.decode("utf-8", "replace").strip())
            if error is not None:
                await self._queue.put(error)

    def _parse(self, raw: str) -> NovaError | None:
        if not raw:
            return None
        try:
            outer = json.loads(raw)
        except json.JSONDecodeError:
            return None

        # Filter by journald PRIORITY (string field)
        priority_str = outer.get("PRIORITY", "7")
        try:
            if int(priority_str) > _MAX_PRIORITY:
                return None
        except ValueError:
            return None

        # The structlog JSON is in MESSAGE
        message_raw = outer.get("MESSAGE", "")
        if not message_raw:
            return None

        return self._parse_message(
            message_raw,
            timestamp=str(outer.get("__REALTIME_TIMESTAMP", "")),
        )

    def _parse_message(self, message_raw: str, *, timestamp: str) -> NovaError | None:
        # MESSAGE may be a plain string or structlog JSON
        try:
            inner = json.loads(message_raw)
        except (json.JSONDecodeError, TypeError):
            return self._parse_plain_text(message_raw, timestamp=timestamp)

        level = str(inner.get("level", "")).lower()
        if level not in ("error", "critical", "warning"):
            return None

        event = str(inner.get("event", "unknown"))
        exc_raw = str(inner.get("exc", inner.get("error", "")))
        exc_type = str(inner.get("exc_type", ""))

        # If exc looks like "ClassName: message", split it
        if not exc_type and ":" in exc_raw:
            exc_type, _, exc_value = exc_raw.partition(":")
            exc_value = exc_value.strip()
        else:
            exc_value = exc_raw

        logger_name = str(inner.get("logger", "unknown"))
        timestamp = str(inner.get("timestamp", outer.get("__REALTIME_TIMESTAMP", "")))

        return NovaError(
            timestamp=timestamp,
            event=event,
            exc_type=exc_type.strip(),
            exc_value=exc_value.strip(),
            logger=logger_name,
            service=self._service,
            level=level,
            raw_json=message_raw,
        )

    def _parse_plain_text(self, message_raw: str, *, timestamp: str) -> NovaError | None:
        message = str(message_raw or "").rstrip()
        if not message:
            return None

        if self._traceback_lines:
            self._traceback_lines.append(message)
            if self._is_traceback_terminal(message):
                block = "\n".join(self._traceback_lines)
                self._traceback_lines = []
                return self._build_traceback_error(block, timestamp=timestamp)
            return None

        if message.startswith("Traceback (most recent call last):"):
            self._traceback_lines = [message]
            return None

        return match_known_error(message, service=self._service)

    @staticmethod
    def _is_traceback_terminal(message: str) -> bool:
        stripped = message.strip()
        if not stripped or stripped.startswith("File "):
            return False
        return bool(
            re.match(r"^[A-Za-z_][\w.]*?(Error|Exception|Warning)(?:: .*)?$", stripped)
            or re.match(r"^[A-Za-z_][\w.]*: .+$", stripped)
        )

    def _build_traceback_error(self, block: str, *, timestamp: str) -> NovaError | None:
        known = match_known_error(block, service=self._service)
        if known is not None:
            if timestamp:
                known.timestamp = timestamp
            return known

        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            return None

        exc_line = lines[-1]
        if ":" in exc_line:
            exc_type, exc_value = exc_line.split(":", 1)
            exc_value = exc_value.strip()
        else:
            exc_type, exc_value = exc_line, ""
        exc_type = exc_type.strip() or "TracebackError"

        logger = "unknown.traceback"
        for match in re.finditer(r'File "([^"]+)"', block):
            file_path = Path(match.group(1))
            try:
                rel = file_path.resolve().relative_to(self._settings.nova_path.resolve())
            except Exception:
                continue
            if rel.suffix == ".py":
                logger = ".".join(rel.with_suffix("").parts)

        event = f"plain_traceback.{exc_type.lower().replace('.', '_')}"
        return NovaError(
            timestamp=timestamp,
            event=event,
            exc_type=exc_type,
            exc_value=exc_value,
            logger=logger,
            service=self._service,
            level="error",
            raw_json=block,
        )
