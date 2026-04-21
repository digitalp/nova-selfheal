"""
BackendHealthChecker — proactive health monitoring for the Nova backend.

Polls the local /health endpoint every CHECK_INTERVAL_S seconds.
After FAIL_THRESHOLD consecutive failures it runs diagnostics and attempts
automatic recovery.  Notifies via Telegram on every state change.

Recovery decision tree:
  1. avatar-backend service not active  → auto-restart (with 5-min cooldown)
  2. nginx not active                   → auto-restart nginx
  3. Both active but /health still down → inject synthetic NovaError into
     the error queue so Claude investigates the root cause
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

import structlog

from nova_selfheal.event_store import EventStore, EVT_AUTO_RESTART
from nova_selfheal.models import NovaError

if TYPE_CHECKING:
    from nova_selfheal.config import Settings
    from nova_selfheal.telegram_bot import TelegramApprovalBot

_LOGGER = structlog.get_logger(__name__)

CHECK_INTERVAL_S = 60       # seconds between healthy polls
FAIL_THRESHOLD = 3          # consecutive failures before action
RETRY_INTERVAL_S = 5        # gap between retries in a confirmation window
RESTART_COOLDOWN_S = 300    # 5-min minimum between auto-restarts per service
STARTUP_GRACE_S = 90        # wait before first check (backend may be starting)


def _svc_is_active(service: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _svc_restart(service: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["sudo", "systemctl", "restart", service],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return True, f"{service} restarted successfully."
        return False, f"exit {r.returncode}: {r.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return False, f"systemctl restart {service} timed out"
    except Exception as exc:
        return False, repr(exc)


def _journal_tail(service: str, lines: int = 40) -> str:
    try:
        r = subprocess.run(
            ["journalctl", "-u", service, "-n", str(lines),
             "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout[-1500:]
    except Exception:
        return "(could not read journal)"


class BackendHealthChecker:
    """Polls /health and auto-recovers the backend when it becomes unreachable."""

    def __init__(
        self,
        settings: "Settings",
        error_queue: "asyncio.Queue[NovaError]",
        events: EventStore,
        bot: "TelegramApprovalBot",
        get_paused: "Callable[[], bool]",
    ) -> None:
        self._settings = settings
        self._queue = error_queue
        self._events = events
        self._bot = bot
        self._get_paused = get_paused
        self._task: asyncio.Task | None = None
        self._last_restart: dict[str, float] = {}
        self._in_outage = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="health_checker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _check_once(self) -> bool:
        """Return True if the health endpoint responds HTTP 200."""
        import aiohttp
        url = f"http://127.0.0.1:{self._settings.backend_port}/health"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def _confirm_down(self) -> bool:
        """Retry FAIL_THRESHOLD times; return True only if all retries fail."""
        for _ in range(FAIL_THRESHOLD):
            if await self._check_once():
                return False
            await asyncio.sleep(RETRY_INTERVAL_S)
        return True

    # ── Main loop ────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        await asyncio.sleep(STARTUP_GRACE_S)
        _LOGGER.info("health_checker.started", interval_s=CHECK_INTERVAL_S)

        while True:
            try:
                ok = await self._check_once()

                if ok:
                    if self._in_outage:
                        self._in_outage = False
                        _LOGGER.info("health_checker.recovered")
                        await self._bot.send_notification(
                            "✅ *Nova backend recovered* — `/health` is responding normally\\."
                        )
                    await asyncio.sleep(CHECK_INTERVAL_S)
                    continue

                # Possible blip — confirm with retries before acting
                if not await self._confirm_down():
                    await asyncio.sleep(CHECK_INTERVAL_S)
                    continue

                if not self._in_outage:
                    self._in_outage = True
                    _LOGGER.warning("health_checker.backend_unreachable")

                if not self._get_paused():
                    await self._diagnose_and_recover()

                await asyncio.sleep(CHECK_INTERVAL_S)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                _LOGGER.warning("health_checker.loop_error", exc=repr(exc))
                await asyncio.sleep(CHECK_INTERVAL_S)

    # ── Diagnostics & recovery ────────────────────────────────────────────────

    async def _diagnose_and_recover(self) -> None:
        now = time.monotonic()
        ts = datetime.now(timezone.utc).isoformat()
        svc = self._settings.watch_service

        # ── 1. Is avatar-backend running? ─────────────────────────────────
        if not _svc_is_active(svc):
            last = self._last_restart.get(svc, 0)
            if now - last < RESTART_COOLDOWN_S:
                wait = int(RESTART_COOLDOWN_S - (now - last))
                _LOGGER.info("health_checker.restart_cooldown", service=svc, wait_s=wait)
                await self._bot.send_notification(
                    f"⚠️ *Nova backend is down*\n\n"
                    f"`{svc}` is not active\\. Restart cooldown: {wait}s remaining\\.\n"
                    f"Manual intervention may be needed\\."
                )
                return

            journal = _journal_tail(svc)
            _LOGGER.warning("health_checker.auto_restart", service=svc)
            ok, msg = _svc_restart(svc)
            self._last_restart[svc] = now
            self._events.record(
                EVT_AUTO_RESTART, service=svc,
                log_event="health_checker.backend_down", message=msg,
            )
            icon = "✅" if ok else "❌"
            await self._bot.send_notification(
                f"{icon} *Nova backend was down — auto\\-restarted*\n\n"
                f"Service `{svc}` was not active\\.\n"
                f"Result: `{_tg_esc(msg)}`\n\n"
                f"*Recent journal:*\n```\n{journal}\n```"
            )
            return

        # ── 2. Is nginx running? ───────────────────────────────────────────
        if not _svc_is_active("nginx"):
            last = self._last_restart.get("nginx", 0)
            if now - last < RESTART_COOLDOWN_S:
                wait = int(RESTART_COOLDOWN_S - (now - last))
                await self._bot.send_notification(
                    f"⚠️ *nginx is down* — admin panel unreachable\\.\n"
                    f"Restart cooldown: {wait}s remaining\\."
                )
                return

            _LOGGER.warning("health_checker.auto_restart", service="nginx")
            ok, msg = _svc_restart("nginx")
            self._last_restart["nginx"] = now
            self._events.record(
                EVT_AUTO_RESTART, service="nginx",
                log_event="health_checker.nginx_down", message=msg,
            )
            icon = "✅" if ok else "❌"
            await self._bot.send_notification(
                f"{icon} *nginx was down — auto\\-restarted*\n"
                f"Result: `{_tg_esc(msg)}`"
            )
            return

        # ── 3. Both services active but /health still failing ─────────────
        # Escalate to Claude for investigation
        _LOGGER.warning("health_checker.services_ok_health_failing")
        journal = _journal_tail(svc, lines=60)
        synthetic = NovaError(
            timestamp=ts,
            event="health_checker.endpoint_unreachable",
            exc_type="HealthCheckFailure",
            exc_value=(
                f"The /health endpoint at 127.0.0.1:{self._settings.backend_port} "
                "is not responding HTTP 200 even though avatar-backend and nginx "
                f"are both active. Recent journal:\n{journal}"
            ),
            logger="avatar_backend.routers.health",
            service=svc,
            level="error",
            raw_json="{}",
        )
        await self._queue.put(synthetic)
        _LOGGER.info(
            "health_checker.queued_for_investigation",
            event=synthetic.event,
        )
        await self._bot.send_notification(
            "🔴 *Nova admin panel unreachable*\n\n"
            "Both `avatar\\-backend` and `nginx` are active but `/health` is not "
            "responding\\. Queued for Claude investigation\\."
        )


def _tg_esc(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text
