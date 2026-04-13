# Nova AI Backend — Architecture Context for Self-Heal Agent

> **Auto-generated context.** Updated by `/opt/nova-selfheal/scripts/update-context.sh`
> whenever Nova source is deployed. Do not edit by hand — changes will be overwritten.

---

## Overview

Nova is a FastAPI AI home assistant running on Ubuntu (x86_64, Coral TPU attached).
- **V1**: `/opt/avatar-server/` — Python package `avatar_backend`, port 8001, systemd unit `avatar-backend`
- Entry point: `avatar_backend.main` — single asyncio event loop managed by uvicorn

All services live on `app.state.*` and follow `async start() / stop()` pattern.

---

## Service Map

### Core Infrastructure
| Service | `app.state.*` | File | Responsibility |
|---------|--------------|------|----------------|
| `LLMService` | `llm_service` | `services/llm_service.py` | Gemini 2.5 Flash + Ollama fallback; `generate()`, `describe_image()` |
| `HAProxy` | `ha_proxy` | `services/ha_proxy.py` | HA REST + WebSocket API; `get_entity_state()`, `call_service()`, `subscribe_events()` |
| `PresenceContextService` | `presence_service` | `services/presence_context.py` | Auto-discovers person.* + binary_sensor presence; injects live note per conversation turn |
| `SessionManager` | `session_manager` | `services/session_manager.py` | Per-connection conversation history; `cleanup_expired()` every 5 min |
| `ConversationService` | `conversation_service` | `services/conversation_service.py` | `_run_turn()` — assembles context, calls `run_chat()`, invokes tools |
| `PersistentMemoryService` | `memory_service` | `services/persistent_memory.py` | Long-term household memory in SQLite (`metrics.db`) |
| `MetricsDB` | `metrics_db` | `services/metrics_db.py` | Central SQLite store for clips, memories, decisions, metrics |
| `EventStoreService` | `event_store` | `services/event_store.py` | Records pipeline events (motion, camera, HA); SSE feed for admin UI |
| `EventBusService` | `event_bus` | `services/event_bus.py` | Internal pub/sub between services |
| `UserService` | `user_service` | `services/user_service.py` | Admin login; users stored in `config/users.json` |
| `ACLManager` | `acl_manager` | `models/acl.py` | Per-entity access control for HA tool calls |
| `CostLog` | `cost_log` | `services/cost_log.py` | Tracks LLM API cost per request |
| `LogStore` | `log_store` | `services/log_store.py` | Tails rotating log file; SSE stream to admin UI |
| `ActionService` | `action_service` | `services/action_service.py` | Executes structured actions (HA calls, TTS, etc.) |
| `PromptSyncService` | `prompt_sync_service` | `services/prompt_sync_service.py` | Syncs `config/system_prompt.txt` from HA input_text helper |
| `SurfaceStateService` | `surface_state_service` | `services/surface_state_service.py` | Tracks avatar surface display state |

### Voice Pipeline
| Service | `app.state.*` | File | Responsibility |
|---------|--------------|------|----------------|
| `STTService` | `stt_service` | `services/stt_service.py` | faster-whisper; `transcribe(audio_bytes)` |
| `TTSService` | `tts_service` | `services/tts_service.py` | Piper / ElevenLabs / IntronAfroTTS (XTTS sidecar at port 8021); `synthesize(text)` |
| `RealtimeVoiceService` | `realtime_voice_service` | `services/realtime_voice_service.py` | WebSocket voice pipeline: audio in → STT → chat → TTS → audio out |
| `CoralWakeDetector` | `coral_wake_detector` | `services/coral_wake_detector.py` | Coral TPU wake-word detection |
| `SpeakerService` | `speaker_service` | `services/speaker_service.py` | Plays TTS audio to HA media_player entities |
| `ConnectionManager` | `ws_manager` | `services/ws_manager.py` | WebSocket registry; `broadcast_json()`, `broadcast_to_voice_json()` |

### Motion & Camera Pipeline
| Service | `app.state.*` | File | Responsibility |
|---------|--------------|------|----------------|
| `ProactiveService` | `proactive_service` | `services/proactive_service.py` | Watches HA events (motion, doors, weather); batches changes; LLM decides whether to announce |
| `CameraEventService` | `camera_event_service` | `services/camera_event_service.py` | Gemini vision analysis of camera snapshots on motion |
| `MotionClipService` | `motion_clip_service` | `services/motion_clip_service.py` | ffmpeg MJPEG clip capture from RTSP; semantic "Find Anything" search; retention cleanup |
| `CameraDiscoveryService` | `camera_discovery` | `services/camera_discovery.py` | Auto-discovers cameras + motion sensors from HA area registry on startup |

### Automation & Monitoring
| Service | `app.state.*` | File | Responsibility |
|---------|--------------|------|----------------|
| `SensorWatchService` | `sensor_watch` | `services/sensor_watch_service.py` | HA WebSocket sensor threshold monitoring; Ollama LLM review; announces anomalies |
| `OpenLoopAutomationService` | `open_loop_automation_service` | `services/open_loop_automation_service.py` | Persistent HA-driven automation workflows |
| `OpenLoopWorkflowService` | `open_loop_workflow_service` | `services/open_loop_workflow_service.py` | Multi-step workflow engine |
| `IssueAutoFixService` | `issue_autofix_service` | `services/issue_autofix_service.py` | Built-in self-healing: restarts watchers on HA timeout |
| `DecisionLog` | `decision_log` | `services/decision_log.py` | Ring-buffer AI decision recorder; SQLite + SSE to admin UI |
| `SystemMetrics` | — | `services/system_metrics.py` | CPU/RAM/disk metrics for admin dashboard |

---

## Routers

| Router | Mount | Key Endpoints |
|--------|-------|---------------|
| `health` | `/health` | `GET /health` — liveness check |
| `chat` | `/chat` | `POST /chat` — REST chat |
| `voice` | `/voice` | WebSocket voice pipeline |
| `avatar_ws` | `/avatar` | WebSocket for avatar frontend |
| `announce` | `/announce` | `POST /announce` — trigger proactive speech |
| `admin` | `/admin` | Sessions, clips, prompts, selfheal proxy, config |

---

## Key Data Flows

### Voice Conversation
```
WebSocket (/voice)
  → RealtimeVoiceService
    → CoralWakeDetector (wake word)
    → STTService.transcribe()
    → ConversationService._run_turn()
      → PresenceContextService (inject presence note)
      → LLMService.generate() [Gemini 2.5 Flash → Ollama fallback]
      → Tool calls → HAProxy → ACLManager.check()
      → PersistentMemoryService (store/recall)
    → TTSService.synthesize()
    → SpeakerService (HA media_player)
    → WS audio response to browser
```

### Motion / Camera Announcement
```
HA WebSocket event (motion_detected / camera_snapshot)
  → ProactiveService._handle_event()
    → batch window (60s) — deduplicate nearby triggers
    → CameraEventService.analyze() — Gemini vision on snapshot
    → MotionClipService.capture() — ffmpeg RTSP clip
    → LLMService.generate() — should we announce?
    → _proactive_announce(message)
      → AnnounceRequest → announce_handler()
        → TTSService.synthesize()
        → SpeakerService.play()
        → WS broadcast to avatar
```

---

## Config (`/opt/avatar-server/.env`)

| Key | Default | Notes |
|-----|---------|-------|
| `LLM_PROVIDER` | `ollama` | `ollama` / `google` / `openai` / `anthropic` |
| `CLOUD_MODEL` | — | Model name for cloud providers |
| `GOOGLE_API_KEY` | — | Gemini API key |
| `OLLAMA_MODEL` | `llama3.1:8b` | Primary text model |
| `OLLAMA_VISION_MODEL` | `llama3.2-vision:11b` | Vision model |
| `PROACTIVE_OLLAMA_MODEL` | — | Override for proactive service |
| `SENSOR_WATCH_OLLAMA_MODEL` | — | Override for sensor watch |
| `HA_URL` | `http://homeassistant.local:8123` | Home Assistant base URL |
| `TTS_PROVIDER` | `piper` | `piper` / `elevenlabs` / `afrotts` / `intron_afro_tts` |
| `INTRON_AFRO_TTS_URL` | `http://127.0.0.1:8021` | XTTS sidecar URL |
| `WHISPER_MODEL` | `small` | STT model size |
| `SPEAKERS` | — | Comma-separated HA media_player entity IDs |
| `PUBLIC_URL` | `http://192.168.0.249:8001` | URL Nova serves audio from |
| `MOTION_CLIP_RETENTION_DAYS` | `30` | Auto-delete clips older than N days |

Config singleton: `from avatar_backend.config import get_settings` (cached via `@lru_cache`).

---

## Coding Conventions

- **Async-first**: all I/O uses `async`/`await`; no blocking calls on the event loop
- **Structured logging**: `structlog.get_logger(__name__)` — first positional arg is the event name:
  `_LOGGER.info("service.thing_happened", key=val)` — never pass `event=` as a keyword arg
- **Config**: `from avatar_backend.config import get_settings` (singleton)
- **Error handling**: catch specific exceptions; log with `exc=repr(exc)`, `exc_type=type(exc).__name__`
- **Timestamps**: `datetime.now()` (local time), never `datetime.now(timezone.utc)`
- **Type hints**: Python 3.12 — `X | None` not `Optional[X]`
- **Service init**: ALL instance attributes must be set in `__init__` — missing attr → `AttributeError` on first use
- **Cooldowns**: `dict[str, float]` keyed by entity_id storing `time.monotonic()` timestamps

---

## Common Error Patterns & Root Causes

| Error event | Logger | Typical cause |
|-------------|--------|---------------|
| `ha_proxy.camera_error` | `ha_proxy` | httpx timeout fetching camera snapshot — add retry or increase timeout |
| `ha_proxy.call_service_failed` | `ha_proxy` | HA returned non-2xx — check entity_id, service domain |
| `coral.check_failed` | `coral_detector` | Coral TPU USB reset — service falls through to vision |
| `proactive.motion_describe_failed` | `proactive_service` | Gemini vision failed — check `GOOGLE_API_KEY`, add retry |
| `sensor_watch.ws_disconnected` | `sensor_watch_service` | HA WebSocket dropped — service auto-reconnects with backoff |
| `heating.shadow_eval_failed` | `proactive_service` | Ollama timeout on `mistral-nemo:12b` — increase `SENSOR_WATCH_REVIEW_TIMEOUT_S` |
| `llm.describe_image_gemini_error` | `llm_service` | Gemini API quota or network error |
| `camera_event.analyze_failed` | `camera_event_service` | Snapshot fetch or LLM vision call failed |
| `motion_clip.capture_failed` | `motion_clip_service` | ffmpeg RTSP error — camera offline or URL changed |
| `realtime_voice.stt_failed` | `realtime_voice_service` | Whisper inference error or bad audio |
| `proactive.announce_failed` | `proactive_service` | TTS or speaker error — check `SPEAKERS`, `TTS_PROVIDER` |
| `AttributeError: 'X' has no attribute 'Y'` | any | Missing `self._y = ...` in `__init__` — add initialisation |

---

## IMPORTANT — You Are in Fix-Proposal Mode

You have been invoked by nova-selfheal to analyse a single ERROR and propose a fix.

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
