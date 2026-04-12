from __future__ import annotations
import asyncio
import io
import time
import uuid
from typing import TYPE_CHECKING, Callable, Awaitable

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

if TYPE_CHECKING:
    from nova_selfheal.config import Settings
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
    """

    def __init__(
        self,
        settings: "Settings",
        on_approve: Callable[[str], Awaitable[None]],
        on_reject: Callable[[str], Awaitable[None]],
    ) -> None:
        self._settings = settings
        self._on_approve = on_approve
        self._on_reject = on_reject
        self._pending: dict[str, "PendingFix"] = {}
        self._app: Application | None = None
        self._timeout_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._app = (
            Application.builder()
            .token(self._settings.telegram_bot_token)
            .build()
        )
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
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
            # Diff too long — send as file attachment
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
            # No diff — analysis only
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

    # ── Internals ─────────────────────────────────────────────────────────────

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
                await self._on_reject(fix_id)
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
