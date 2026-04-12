# Nova AI Backend — Architecture Context for Self-Heal Agent

## Overview
Nova is a FastAPI-based AI home assistant with two instances:
- **V1**: `/opt/avatar-server/` — Python package `avatar_backend`, port 8001, has Coral TPU
- **V2**: `/opt/nova-v2/` — same codebase, port 8011, Coral disabled

## Service Pattern
All background services follow an async `start()` / `stop()` pattern:
- `start()` creates an `asyncio.Task` and returns immediately
- `stop()` cancels the task and awaits graceful teardown
- Services are wired in `avatar_backend/main.py` lifespan context manager

## Key Files
```
avatar_backend/
├── main.py                    # FastAPI app, lifespan startup/shutdown, structlog config
├── config.py                  # pydantic-settings Settings class, reads .env
├── services/
│   ├── proactive_service.py   # Motion/event-triggered announcements, Coral pre-filter
│   ├── ha_proxy.py            # Home Assistant REST + WebSocket API wrapper
│   ├── camera_event_service.py# Gemini vision analysis of motion snapshots
│   ├── coral_detector.py      # Coral TPU object detection (person/car/animal)
│   ├── motion_clip_service.py # ffmpeg MJPEG clip capture + Find Anything archive
│   ├── decision_log.py        # Ring-buffer AI decision recorder (SQLite + SSE)
│   ├── issue_autofix_service.py # Existing self-healing (restarts watchers on HA timeout)
│   ├── sensor_watch_service.py  # HA WebSocket sensor threshold monitoring
│   ├── llm_service.py         # Gemini 2.5 Flash + Ollama fallback abstraction
│   └── metrics_db.py          # SQLite metrics, recent_decisions() for admin UI
├── routers/
│   ├── admin.py               # Admin REST API, /admin/prompts, /admin/clips
│   └── announce.py            # /announce endpoint for proactive speech
└── static/
    └── admin.html             # Admin UI (dark/light mode, Find Anything, decision log)
```

## Coding Conventions
- **Async-first**: all I/O uses `async`/`await`; no blocking calls on the event loop
- **Structured logging**: `structlog.get_logger(__name__)` with keyword args — no `print()`
- **Config**: `from avatar_backend.config import settings` (singleton pydantic-settings)
- **Error handling**: catch specific exceptions; log with `exc=repr(exc)` and `exc_type=type(exc).__name__`
- **Timestamps**: always `datetime.now()` (local time), never `datetime.now(timezone.utc)`
- **Type hints**: Python 3.12 style; `X | None` not `Optional[X]`

## Common Error Patterns
- `ha_proxy.camera_error` — httpx exception fetching camera snapshot; exc often empty string (RemoteProtocolError)
- `coral.check_failed` — Coral TPU inference failed; falling through to vision
- `proactive.motion_describe_failed` — Gemini vision call failed
- `sensor_watch.ws_disconnected` — HA WebSocket dropped; service auto-reconnects
- `heating.shadow_eval_failed` — Ollama timeout during shadow eval (mistral-nemo:12b)
- `llm.describe_image_gemini_error` — Gemini API error during image description

## IMPORTANT — You Are in Fix-Proposal Mode

You have been invoked by the nova-selfheal agent to analyse a single ERROR and propose a fix.

**Rules:**
1. Only modify the **single source file** shown in the prompt. Do not suggest changes to other files, config, or dependencies.
2. Propose the **minimal fix** — smallest change that resolves the error.
3. Your response MUST follow this exact format:

```
SUMMARY:
<one paragraph explaining the root cause and what the fix does>

DIFF:
```diff
--- a/<relative/path/to/file.py>
+++ b/<relative/path/to/file.py>
@@ -<line>,<count> +<line>,<count> @@
 <context line>
-<removed line>
+<added line>
 <context line>
```
```

4. The diff must apply cleanly with `patch -p1` from `/opt/avatar-server/`
5. If you cannot determine a safe fix, output SUMMARY with your analysis and omit the DIFF section entirely — do NOT guess.
6. Do not add unnecessary imports, comments, or refactoring beyond the fix.
