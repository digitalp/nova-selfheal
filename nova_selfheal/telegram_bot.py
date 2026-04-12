from __future__ import annotations
import asyncio
import io
import time
import uuid
from typing import TYPE_CHECKING, Callable, Awaitable

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

if TYPE_CHECKING:
    from nova_selfheal.config import Settings
    from nova_selfheal.event_store import EventStore
    from nova_selfheal.models import NovaError, PendingFix

_LOGGER = structlog.get_logger(__name__)

# Telegram message limit
_MAX_DIFF_INLINE = 2800


class TelegramApprovalBot:
    """
    Long-polling Telegram bot that:
    - Sends error + proposed diff to the user with Approve/Reject inline keyboard
    - Handles approval callback → calls on_approve(fix_id)
    - Handles rejection callback → calls on_reject(fix_id)
    - Auto-rejects pending fixes after approval_timeout_seconds
    - Responds to /status /pending /config /pause /resume /logs commands
    """

    def __init__(
        self,
        settings: "Settings",
        on_approve: Callable[[str], Awaitable[None]],
        on_reject: Callable[[str], Awaitable[None]],
        event_store: "EventStore | None" = None,
        pending_fixes: "dict[str, PendingFix] | None" = None,
        get_paused: "Callable[[], bool] | None" = None,
        set_paused: "Callable[[bool], None] | None" = None,
    ) -> None:
        self._settings = settings
        self._on_approve = on_approve
        self._on_reject = on_reject
        self._event_store = event_store
        self._pending_fixes = pending_fixes if pending_fixes is not None else {}
        self._get_paused = get_paused or (lambda: False)
        self._set_paused = set_paused or (lambda v: None)
        self._pending: dict[str, "PendingFix"] = {}
        self._app: Application | None = None
        self._timeout_task: asyncio.Task | None = None
        self._start_time = time.monotonic()

    async def start(self) -> None:
        self._app = (
            Application.builder()
            .token(self._settings.telegram_bot_token)
            .build()
        )
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        self._app.add_handler(CommandHandler("status",  self._cmd_status))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))
        self._app.add_handler(CommandHandler("config",  self._cmd_config))
        self._app.add_handler(CommandHandler("pause",   self._cmd_pause))
        self._app.add_handler(CommandHandler("resume",  self._cmd_resume))
        self._app.add_handler(CommandHandler("logs",    self._cmd_logs))
        self._app.add_handler(CommandHandler("help",    self._cmd_help))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        self._timeout_task = asyncio.create_task(
            self._timeout_loop(), name="telegram_timeout_checker"
        )
        _LOGGER.info("telegram_bot.started")

    async def stop(self) -> None:
        if self._timeout_task:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        _LOGGER.info("telegram_bot.stopped")

    # ── Fix proposal messages ─────────────────────────────────────────────────

    async def send_fix_proposal(self, fix: "PendingFix") -> None:
        """Send error details + diff to user with Approve/Reject buttons."""
        fix_id = str(uuid.uuid4())
        fix.fix_id = fix_id  # type: ignore[attr-defined]
        self._pending[fix_id] = fix

        header = (
            f"🔴 *Nova ERROR detected*\n\n"
            f"*Event:* `{_esc(fix.error.event)}`\n"
            f"*Exception:* `{_esc(fix.error.exc_type or 'unknown')}`\n"
            f"*File:* `{_esc(fix.source_file)}`\n\n"
            f"*Claude's analysis:*\n{_esc(fix.summary)}\n"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{fix_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{fix_id}"),
        ]])

        assert self._app
        bot = self._app.bot

        if fix.has_diff and len(fix.diff) <= _MAX_DIFF_INLINE:
            text = header + f"\n*Proposed diff:*\n```diff\n{_esc(fix.diff)}\n```\n\n_Timeout: 30 min_"
            msg = await bot.send_message(
                chat_id=self._settings.telegram_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
        elif fix.has_diff:
            text = header + "\n_Diff attached as file \\(too long for inline\\)_\n\n_Timeout: 30 min_"
            await bot.send_message(
                chat_id=self._settings.telegram_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            diff_bytes = io.BytesIO(fix.diff.encode("utf-8"))
            diff_bytes.name = f"nova_fix_{fix.error.event.replace('.', '_')}.patch"
            msg = await bot.send_document(
                chat_id=self._settings.telegram_chat_id,
                document=diff_bytes,
                reply_markup=keyboard,
            )
        else:
            text = (
                header +
                "\n⚠️ _Claude could not produce a safe diff \\— analysis only\\._\n"
                "\n_Tap Reject to dismiss\\._"
            )
            msg = await bot.send_message(
                chat_id=self._settings.telegram_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Dismiss", callback_data=f"reject:{fix_id}"),
                ]]),
            )

        fix.telegram_message_id = msg.message_id  # type: ignore[attr-defined]
        _LOGGER.info("telegram_bot.proposal_sent", fix_id=fix_id, event=fix.error.event)

    async def send_analysis_only(self, error: "NovaError", summary: str) -> None:
        """Notify user of an error where no valid diff was produced."""
        text = (
            f"🟡 *Nova ERROR — analysis only*\n\n"
            f"*Event:* `{_esc(error.event)}`\n"
            f"*Exception:* `{_esc(error.exc_type or 'unknown')}`\n\n"
            f"*Analysis:*\n{_esc(summary)}\n\n"
            f"_No safe fix could be proposed\\._"
        )
        assert self._app
        await self._app.bot.send_message(
            chat_id=self._settings.telegram_chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def send_result(self, fix_id: str, success: bool, message: str) -> None:
        """Send the outcome of an apply attempt."""
        icon = "✅" if success else "❌"
        text = f"{icon} *Nova Self\\-Heal Result*\n\n{_esc(message)}"
        assert self._app
        await self._app.bot.send_message(
            chat_id=self._settings.telegram_chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        text = (
            "🛡 *Nova Self\\-Heal Commands*\n\n"
            "/status — service status and stats\n"
            "/pending — list pending fixes awaiting approval\n"
            "/config — show current configuration\n"
            "/logs — last 10 pipeline events\n"
            "/pause — stop processing new errors\n"
            "/resume — resume processing errors\n"
            "/help — show this message"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        uptime_s = int(time.monotonic() - self._start_time)
        paused = self._get_paused()
        stats = self._event_store.get_stats() if self._event_store else {}

        h, m = divmod(uptime_s // 60, 60)
        uptime_str = f"{h}h {m}m" if h else f"{m}m {uptime_s % 60}s"

        status_icon = "⏸" if paused else "✅"
        status_label = "Paused" if paused else "Running"

        text = (
            f"🛡 *Nova Self\\-Heal Status*\n\n"
            f"{status_icon} *Status:* {_esc(status_label)}\n"
            f"⏱ *Uptime:* {_esc(uptime_str)}\n"
            f"👁 *Watching:* `{_esc(self._settings.watch_service)}`\n"
            f"⏳ *Pending fixes:* {len(self._pending)}\n\n"
            f"📊 *Totals:*\n"
            f"  Errors detected: {stats.get('errors_detected', '—')}\n"
            f"  Patches applied: {stats.get('patches_applied', '—')}\n"
            f"  Fixes rejected: {stats.get('fixes_rejected', '—')}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self._pending:
            await update.message.reply_text("✅ No pending fixes\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        lines = [f"⏳ *{len(self._pending)} pending fix(es):*\n"]
        for fix_id, fix in self._pending.items():
            age_m = int((time.monotonic() - fix.created_at) / 60)
            lines.append(
                f"• `{_esc(fix.error.event)}` \\({_esc(fix.error.exc_type or '?')}\\) "
                f"— {age_m}m ago"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        s = self._settings
        token = s.telegram_bot_token
        masked = token[:6] + "\\.\\.\\." + token[-4:] if len(token) > 10 else "\\*\\*\\*"
        text = (
            f"⚙️ *Configuration*\n\n"
            f"*Watch service:* `{_esc(s.watch_service)}`\n"
            f"*Nova path:* `{_esc(str(s.nova_path))}`\n"
            f"*Approval timeout:* {s.approval_timeout_seconds}s\n"
            f"*Dedup window:* {s.dedup_window_seconds}s\n"
            f"*Claude timeout:* {s.claude_timeout_seconds}s\n"
            f"*API port:* {s.api_port}\n"
            f"*Log level:* {_esc(s.log_level)}\n"
            f"*Bot token:* `{masked}`\n"
            f"*Chat ID:* `{s.telegram_chat_id}`"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        self._set_paused(True)
        _LOGGER.info("telegram_bot.paused_via_command")
        await update.message.reply_text(
            "⏸ *Self\\-heal paused*\\. New errors will be ignored until you send /resume\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        self._set_paused(False)
        _LOGGER.info("telegram_bot.resumed_via_command")
        await update.message.reply_text(
            "▶️ *Self\\-heal resumed*\\. Monitoring errors again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def _cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self._event_store:
            await update.message.reply_text("_No event store available\\._", parse_mode=ParseMode.MARKDOWN_V2)
            return
        events = self._event_store.get_recent(limit=10)
        if not events:
            await update.message.reply_text("_No events yet\\._", parse_mode=ParseMode.MARKDOWN_V2)
            return
        from nova_selfheal.event_store import (
            EVT_ERROR_DETECTED, EVT_FIX_PROPOSED, EVT_APPLY_OK,
            EVT_APPLY_FAILED, EVT_APPROVED, EVT_REJECTED, EVT_AUTO_REJECTED,
        )
        ICONS = {
            EVT_ERROR_DETECTED: "🔴", EVT_FIX_PROPOSED: "🔧",
            EVT_APPLY_OK: "✅", EVT_APPLY_FAILED: "💥",
            EVT_APPROVED: "✅", EVT_REJECTED: "❌", EVT_AUTO_REJECTED: "⏱",
        }
        lines = ["📋 *Last 10 events:*\n"]
        for e in events:
            icon = ICONS.get(e["event_type"], "•")
            ts = e.get("ts_iso", "")[:16].replace("T", " ") if e.get("ts_iso") else "—"
            label = e["event_type"].replace("_", "\\_")
            detail = _esc((e.get("log_event") or e.get("service") or "")[:40])
            lines.append(f"{icon} `{_esc(ts)}` {label} {detail}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _is_authorized(self, update: Update) -> bool:
        """Only respond to messages from the configured chat ID."""
        if not update.message:
            return False
        return update.message.chat_id == self._settings.telegram_chat_id

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()

        action, _, fix_id = query.data.partition(":")
        fix = self._pending.pop(fix_id, None)

        if fix is None:
            await query.edit_message_text("⚠️ This fix has already been handled or expired.")
            return

        if action == "approve":
            await query.edit_message_text("⏳ Applying fix...")
            await self._on_approve(fix_id)
        else:
            await query.edit_message_text("❌ Fix rejected.")
            await self._on_reject(fix_id)

    async def _timeout_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await self._check_timeouts()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                _LOGGER.warning("telegram_bot.timeout_loop_error", exc=repr(exc))

    async def _check_timeouts(self) -> None:
        now = time.monotonic()
        expired = [
            fix_id for fix_id, fix in self._pending.items()
            if now - fix.created_at > self._settings.approval_timeout_seconds
        ]
        for fix_id in expired:
            fix = self._pending.pop(fix_id, None)
            if fix:
                _LOGGER.info("telegram_bot.auto_rejected", fix_id=fix_id, event=fix.error.event)
                await self._on_reject(fix_id, auto=True)
                assert self._app
                await self._app.bot.send_message(
                    chat_id=self._settings.telegram_chat_id,
                    text=f"⏱ Fix for `{_esc(fix.error.event)}` auto\\-rejected \\(30 min timeout\\)\\.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )


def _esc(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))
