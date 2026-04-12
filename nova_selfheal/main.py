from __future__ import annotations
import asyncio
import signal
import time
import uuid

import structlog

from nova_selfheal.config import Settings
from nova_selfheal.context_builder import ContextBuilder
from nova_selfheal.claude_agent import ClaudeAgent
from nova_selfheal.deduplicator import ErrorDeduplicator
from nova_selfheal.models import NovaError, PendingFix
from nova_selfheal.patch_applier import PatchApplier
from nova_selfheal.patch_extractor import extract_diff, extract_summary, validate_diff
from nova_selfheal.telegram_bot import TelegramApprovalBot
from nova_selfheal.watcher import JournalWatcher

_LOGGER = structlog.get_logger(__name__)

# fix_id → PendingFix for in-flight approvals
_pending_fixes: dict[str, PendingFix] = {}


def _configure_logging(level: str) -> None:
    import logging
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO))


async def run(settings: Settings) -> None:
    queue: asyncio.Queue[NovaError] = asyncio.Queue()

    dedup = ErrorDeduplicator(settings.db_path, settings.dedup_window_seconds)
    dedup.setup()

    builder = ContextBuilder(settings.nova_path)
    agent = ClaudeAgent(settings.claude_work_dir, settings)
    applier = PatchApplier(settings.nova_path, settings.watch_service)

    # ── Approval callbacks ────────────────────────────────────────────────────

    async def on_approve(fix_id: str) -> None:
        fix = _pending_fixes.pop(fix_id, None)
        if fix is None:
            return
        if not fix.has_diff:
            await bot.send_result(fix_id, False, "No diff to apply.")
            return

        ok, msg = await applier.apply(fix.diff)
        if ok:
            restart_ok, restart_msg = await applier.restart_service()
            full_msg = f"Patch applied.\n{restart_msg}"
            await bot.send_result(fix_id, restart_ok, full_msg)
        else:
            await bot.send_result(fix_id, False, msg)

    async def on_reject(fix_id: str) -> None:
        _pending_fixes.pop(fix_id, None)
        _LOGGER.info("selfheal.fix_rejected", fix_id=fix_id)

    # ── Bot ───────────────────────────────────────────────────────────────────

    bot = TelegramApprovalBot(settings, on_approve=on_approve, on_reject=on_reject)
    watcher = JournalWatcher(settings.watch_service, queue, settings)

    await bot.start()
    await watcher.start()
    _LOGGER.info("selfheal.started", service=settings.watch_service)

    try:
        while True:
            error: NovaError = await queue.get()

            _LOGGER.info("selfheal.error_received", event=error.event, exc_type=error.exc_type)

            # Deduplicate
            if dedup.is_duplicate(error):
                _LOGGER.info("selfheal.deduplicated", event=error.event)
                continue
            dedup.record(error)

            # Resolve source file
            source_file = builder.resolve_source_file(error.logger)
            if source_file is None:
                _LOGGER.warning("selfheal.no_source_file", logger=error.logger)
                await bot.send_analysis_only(
                    error,
                    f"Could not resolve source file for logger `{error.logger}`. Manual investigation needed."
                )
                continue

            # Build context and invoke Claude
            error_context = builder.build_prompt_context(error, source_file)
            try:
                claude_output = await agent.generate_fix(error, error_context)
            except RuntimeError as exc:
                _LOGGER.error("selfheal.claude_failed", exc=repr(exc))
                await bot.send_analysis_only(error, f"Claude invocation failed: {exc}")
                continue

            # Extract and validate
            summary = extract_summary(claude_output)
            diff = extract_diff(claude_output)

            if diff and not validate_diff(diff, settings.nova_path):
                _LOGGER.warning("selfheal.unsafe_diff", event=error.event)
                diff = None  # Treat as analysis-only

            fix_id = str(uuid.uuid4())
            fix = PendingFix(
                fix_id=fix_id,
                error=error,
                source_file=str(source_file.relative_to(settings.nova_path)),
                diff=diff or "",
                summary=summary,
                created_at=time.monotonic(),
            )
            _pending_fixes[fix_id] = fix

            if fix.has_diff:
                await bot.send_fix_proposal(fix)
            else:
                _LOGGER.info("selfheal.no_diff_analysis_only", event=error.event)
                await bot.send_analysis_only(error, summary)

    except asyncio.CancelledError:
        _LOGGER.info("selfheal.stopping")
    finally:
        await watcher.stop()
        await bot.stop()
        dedup.close()
        _LOGGER.info("selfheal.stopped")


def main() -> None:
    settings = Settings()
    _configure_logging(settings.log_level)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(run(settings))

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)

    try:
        loop.run_until_complete(task)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
