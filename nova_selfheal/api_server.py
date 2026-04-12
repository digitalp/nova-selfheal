from __future__ import annotations
import asyncio
import json
import time
from typing import TYPE_CHECKING, Callable, Awaitable

import structlog
from aiohttp import web

if TYPE_CHECKING:
    from nova_selfheal.config import Settings
    from nova_selfheal.event_store import EventStore
    from nova_selfheal.models import PendingFix

_LOGGER = structlog.get_logger(__name__)

_START_TIME = time.monotonic()


def _cors(response: web.Response) -> web.Response:
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def create_app(
    settings: "Settings",
    event_store: "EventStore",
    pending_fixes: "dict[str, PendingFix]",
    on_approve: Callable[[str], Awaitable[None]],
    on_reject: Callable[[str], Awaitable[None]],
) -> web.Application:
    app = web.Application()

    async def handle_status(request: web.Request) -> web.Response:
        uptime_s = int(time.monotonic() - _START_TIME)
        stats = event_store.get_stats()
        data = {
            "status": "running",
            "uptime_seconds": uptime_s,
            "watch_service": settings.watch_service,
            "nova_path": str(settings.nova_path),
            "pending_count": len(pending_fixes),
            **stats,
        }
        return _cors(web.Response(
            text=json.dumps(data),
            content_type="application/json",
        ))

    async def handle_events(request: web.Request) -> web.Response:
        limit = int(request.rel_url.query.get("limit", "100"))
        events = event_store.get_recent(limit=min(limit, 500))
        # Don't send full diffs in the list — too large
        for e in events:
            if len(e.get("diff", "")) > 200:
                e["diff"] = e["diff"][:200] + "\n... (truncated)"
        return _cors(web.Response(
            text=json.dumps(events),
            content_type="application/json",
        ))

    async def handle_pending(request: web.Request) -> web.Response:
        result = []
        for fix_id, fix in list(pending_fixes.items()):
            result.append({
                "fix_id": fix_id,
                "event": fix.error.event,
                "exc_type": fix.error.exc_type,
                "source_file": fix.source_file,
                "summary": fix.summary,
                "has_diff": fix.has_diff,
                "diff": fix.diff if fix.has_diff else "",
                "age_seconds": int(time.monotonic() - fix.created_at),
            })
        return _cors(web.Response(
            text=json.dumps(result),
            content_type="application/json",
        ))

    async def handle_config(request: web.Request) -> web.Response:
        token = settings.telegram_bot_token
        masked_token = token[:6] + "..." + token[-4:] if len(token) > 10 else "***"
        data = {
            "watch_service": settings.watch_service,
            "nova_path": str(settings.nova_path),
            "claude_work_dir": str(settings.claude_work_dir),
            "approval_timeout_seconds": settings.approval_timeout_seconds,
            "dedup_window_seconds": settings.dedup_window_seconds,
            "claude_timeout_seconds": settings.claude_timeout_seconds,
            "api_port": settings.api_port,
            "log_level": settings.log_level,
            "telegram_bot_token": masked_token,
            "telegram_chat_id": settings.telegram_chat_id,
        }
        return _cors(web.Response(
            text=json.dumps(data),
            content_type="application/json",
        ))

    async def handle_approve(request: web.Request) -> web.Response:
        fix_id = request.match_info["fix_id"]
        if fix_id not in pending_fixes:
            return _cors(web.Response(
                status=404,
                text=json.dumps({"error": "fix not found or already handled"}),
                content_type="application/json",
            ))
        asyncio.create_task(on_approve(fix_id))
        return _cors(web.Response(
            text=json.dumps({"ok": True, "fix_id": fix_id}),
            content_type="application/json",
        ))

    async def handle_reject(request: web.Request) -> web.Response:
        fix_id = request.match_info["fix_id"]
        if fix_id not in pending_fixes:
            return _cors(web.Response(
                status=404,
                text=json.dumps({"error": "fix not found or already handled"}),
                content_type="application/json",
            ))
        asyncio.create_task(on_reject(fix_id))
        return _cors(web.Response(
            text=json.dumps({"ok": True, "fix_id": fix_id}),
            content_type="application/json",
        ))

    async def handle_options(request: web.Request) -> web.Response:
        return web.Response(
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )

    app.router.add_get("/status", handle_status)
    app.router.add_get("/events", handle_events)
    app.router.add_get("/pending", handle_pending)
    app.router.add_get("/config", handle_config)
    app.router.add_post("/pending/{fix_id}/approve", handle_approve)
    app.router.add_post("/pending/{fix_id}/reject", handle_reject)
    app.router.add_route("OPTIONS", "/{path_info:.*}", handle_options)

    return app


class ApiServer:
    def __init__(self, app: web.Application, host: str, port: int) -> None:
        self._app = app
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        _LOGGER.info("api_server.started", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        _LOGGER.info("api_server.stopped")
