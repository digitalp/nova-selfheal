from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path

from nova_selfheal.models import NovaError

_WEBSOCKET_CONTAINER_EVENT = "websocket.container_dependency_error"
_WEBSOCKET_CONTAINER_LOGGER = "avatar_backend.bootstrap.container"
_WEBSOCKET_CONTAINER_MARKER = "get_container() missing 1 required positional argument"
_ATTRIBUTE_INIT_RE = re.compile(r"'(?P<class_name>[A-Za-z_]\w*)' object has no attribute '(?P<attr>[A-Za-z_]\w*)'")
_PROMPT_GAP_RE = re.compile(r"\b(can(?:not|'t)\s+access|don't\s+have\s+access|unable\s+to\s+access)\b", re.I)
_PROMPT_GAP_HINTS = ("weather", "integration", "home assistant", "entity", "camera", "music assistant")


@dataclass
class KnownFixProposal:
    summary: str
    diff: str
    verification_mode: str = "basic"


def match_known_error(message: str, *, service: str) -> NovaError | None:
    """Convert known plain-text regressions into synthetic NovaError records."""
    if _WEBSOCKET_CONTAINER_MARKER not in message:
        return None
    return NovaError(
        timestamp="",
        event=_WEBSOCKET_CONTAINER_EVENT,
        exc_type="TypeError",
        exc_value=message.strip(),
        logger=_WEBSOCKET_CONTAINER_LOGGER,
        service=service,
        level="error",
        raw_json=message,
    )


def propose_known_fix(error: NovaError, source_file: Path) -> KnownFixProposal | None:
    """Return a deterministic fix for known regression signatures."""
    for proposer in (
        _propose_websocket_container_fix,
        _propose_attribute_init_fix,
        _propose_gemini_429_fix,
        _propose_motion_clip_concurrency_fix,
        _propose_polling_only_camera_fix,
        _propose_prompt_gap_fix,
    ):
        proposal = proposer(error, source_file)
        if proposal is not None:
            return proposal
    return None


def _propose_websocket_container_fix(error: NovaError, source_file: Path) -> KnownFixProposal | None:
    if error.event != _WEBSOCKET_CONTAINER_EVENT or source_file.name != "container.py":
        return None

    original = source_file.read_text(encoding="utf-8")
    if "from starlette.requests import HTTPConnection" in original and "def get_container(connection: HTTPConnection)" in original:
        return KnownFixProposal(
            summary=(
                "The websocket routes are using Depends(get_container), but the helper was typed "
                "for Request-only injection. FastAPI resolves websocket dependencies with an "
                "HTTPConnection-compatible object, so websocket connection setup fails before the "
                "route body runs. The fix is already present: get_container() now accepts "
                "HTTPConnection for both HTTP and websocket routes."
            ),
            diff="",
            verification_mode="websocket_container",
        )

    updated = original.replace("from fastapi import Request", "from starlette.requests import HTTPConnection")
    updated = updated.replace(
        'def get_container(request: Request) -> "AppContainer":\n    """FastAPI Depends() — extract the typed AppContainer from app.state."""\n    return request.app.state._container\n',
        'def get_container(connection: HTTPConnection) -> "AppContainer":\n    """FastAPI Depends() — extract the typed AppContainer from app.state.\n\n    Accepts both HTTP requests and websocket connections.\n    """\n    return connection.app.state._container\n',
    )
    if updated == original:
        return None

    return KnownFixProposal(
        summary=(
            "The websocket routes are using Depends(get_container), but the helper was typed "
            "for Request-only injection. FastAPI resolves websocket dependencies with an "
            "HTTPConnection-compatible object, so websocket connection setup fails before the "
            "route body runs. The fix is to accept HTTPConnection in get_container() so the "
            "same dependency works for both HTTP and websocket routes."
        ),
        diff=_build_diff(source_file, original, updated),
        verification_mode="websocket_container",
    )


def _propose_attribute_init_fix(error: NovaError, source_file: Path) -> KnownFixProposal | None:
    if source_file.suffix != ".py":
        return None
    if error.exc_type != "AttributeError" and "attributeerror" not in error.event.lower():
        return None

    match = _ATTRIBUTE_INIT_RE.search(error.exc_value or error.raw_json)
    if not match:
        return None

    class_name = match.group("class_name")
    attr_name = match.group("attr")
    original = source_file.read_text(encoding="utf-8")
    module = _safe_parse(original)
    if module is None:
        return None

    class_node = next((node for node in module.body if isinstance(node, ast.ClassDef) and node.name == class_name), None)
    if class_node is None:
        return None

    if _class_assigns_attribute(class_node, attr_name):
        return KnownFixProposal(
            summary=(
                f"{class_name}.{attr_name} is already assigned somewhere in the class, so this "
                "AttributeError is not a missing-init regression. The deterministic init-fix rule "
                "is declining to patch automatically because the attribute exists and the failure "
                "likely comes from control flow or a different object type."
            ),
            diff="",
        )

    init_fn = next((node for node in class_node.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"), None)
    if init_fn is None or not init_fn.body:
        return KnownFixProposal(
            summary=(
                f"The traceback shows `{class_name}` missing `{attr_name}`, but `{class_name}` "
                "does not have a normal __init__ body that the deterministic init-fix rule can "
                "edit safely. Manual review is needed."
            ),
            diff="",
        )

    lines = original.splitlines(keepends=True)
    insert_after = max(getattr(stmt, "end_lineno", stmt.lineno) for stmt in init_fn.body)
    indent = " " * (init_fn.col_offset + 4)
    assignment = f"{indent}self.{attr_name} = None\n"
    if assignment in original:
        return KnownFixProposal(
            summary=(
                f"{class_name} already initializes `{attr_name}` to a default, so this "
                "AttributeError is not fixable with the deterministic init-fix rule."
            ),
            diff="",
        )

    lines.insert(insert_after, assignment)
    updated = "".join(lines)
    return KnownFixProposal(
        summary=(
            f"The traceback shows `{class_name}` using `self.{attr_name}` before it is initialized. "
            f"The deterministic fix adds `self.{attr_name} = None` in `{class_name}.__init__()` so "
            "the instance always has the attribute before later methods read it."
        ),
        diff=_build_diff(source_file, original, updated),
    )


def _propose_gemini_429_fix(error: NovaError, source_file: Path) -> KnownFixProposal | None:
    if source_file.name != "llm_service.py" or not _looks_like_gemini_429(error):
        return None

    original = source_file.read_text(encoding="utf-8")
    required_snippets = (
        'if _VISION_SEMAPHORE.locked():',
        'return await self._fallback_to_ollama_vision(image_bytes, prompt)',
        'structlog.get_logger().warning("gemini_pool.retrying_next_key"',
        'return await _ollama_describe_image(image_bytes, _vision_ollama_url(), settings.ollama_vision_model, prompt or _DEFAULT_IMAGE_PROMPT)',
    )
    if all(snippet in original for snippet in required_snippets):
        return KnownFixProposal(
            summary=(
                "The Gemini 429 fallback guards are already present in llm_service.py: a full semaphore "
                "falls back immediately, three 429s rotate through the key pool, and final fallback uses "
                "_vision_ollama_url() so remote Ollama vision is honored. No deterministic patch is needed."
            ),
            diff="",
        )

    updated = original
    updated = _replace_method(
        updated,
        "LLMService",
        "describe_image_with_gemini",
        """    async def describe_image_with_gemini(self, image_bytes: bytes, prompt: str | None = None, system_instruction: str | None = None, camera_id: str | None = None) -> str:
        \"\"\"
        Describe a camera image using Gemini vision, regardless of the active LLM provider.
        Uses the key pool for rotation across multiple API keys.
        Falls back to Ollama vision if all keys exhausted.
        Limited to 2 concurrent calls to prevent server overload.
        \"\"\"
        # Non-blocking: if 2 vision calls already in-flight, fall back immediately
        if _VISION_SEMAPHORE.locked():
            structlog.get_logger().warning("gemini_pool.vision_busy")
            return await self._fallback_to_ollama_vision(image_bytes, prompt)

        async with _VISION_SEMAPHORE:
            settings = get_settings()
            model = settings.cloud_model if settings.llm_provider.lower() == "google" else _DEFAULT_MODELS["google"]
            _prompt = prompt or _DEFAULT_IMAGE_PROMPT

            # Try up to 3 different keys from the pool
            for _attempt in range(3):
                api_key = _get_gemini_key(camera_id)
                if not api_key:
                    break
                try:
                    return await _gemini_describe_image(image_bytes, api_key, model, _prompt, system_instruction)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429:
                        _report_gemini_429(api_key)
                        structlog.get_logger().warning("gemini_pool.retrying_next_key", attempt=_attempt + 1)
                        continue
                    raise

            # All keys exhausted — fall back to Ollama
            return await self._fallback_to_ollama_vision(image_bytes, prompt)
""",
    )
    updated = _replace_method(
        updated,
        "LLMService",
        "_fallback_to_ollama_vision",
        """    async def _fallback_to_ollama_vision(self, image_bytes: bytes, prompt: str | None = None) -> str:
        try:
            settings = get_settings()
            structlog.get_logger().info("llm.describe_image_gemini_to_ollama_fallback")
            return await _ollama_describe_image(image_bytes, _vision_ollama_url(), settings.ollama_vision_model, prompt or _DEFAULT_IMAGE_PROMPT)
        except Exception as fb_exc:
            structlog.get_logger().error("llm.describe_image_all_failed", exc=str(fb_exc))
            return "I couldn't analyze the camera image right now."
""",
    )
    if updated == original:
        return None

    return KnownFixProposal(
        summary=(
            "The Gemini vision path hit repeated 429s without reliably bailing out to Ollama. "
            "This deterministic fix reinstates the safe behavior: semaphore saturation falls back "
            "immediately, key-pool rotation stops after three 429s, and the final fallback always "
            "uses _vision_ollama_url() so remote vision routing does not re-enter the failing provider."
        ),
        diff=_build_diff(source_file, original, updated),
    )


def _propose_motion_clip_concurrency_fix(error: NovaError, source_file: Path) -> KnownFixProposal | None:
    if source_file.name != "motion_clip_service.py" or not _looks_like_motion_clip_pressure(error):
        return None

    original = source_file.read_text(encoding="utf-8")
    if all(
        snippet in original
        for snippet in (
            "self._capture_semaphore: asyncio.Semaphore | None = None",
            'task_name = f"motion_clip:{camera_entity_id}"',
            '"motion_clip.capture_skipped_already_running"',
            "if self._capture_semaphore is None:",
            "async with self._capture_semaphore:",
        )
    ):
        return KnownFixProposal(
            summary=(
                "Motion clip backpressure guards are already present: per-camera task deduplication, "
                "a global capture semaphore, and polling fallbacks. The deterministic concurrency rule "
                "is not applying another patch. If these errors continue under load, increase the service "
                "memory budget as an operator action, e.g. set `MemoryMax=` for `avatar-backend.service`."
            ),
            diff="",
        )

    updated = original
    if "self._capture_semaphore: asyncio.Semaphore | None = None" not in updated:
        updated = updated.replace(
            "        self._POLLING_ONLY_CAMERAS = set(getattr(_rt, 'polling_only_cameras', []))\n",
            "        self._POLLING_ONLY_CAMERAS = set(getattr(_rt, 'polling_only_cameras', []))\n"
            "        self._capture_semaphore: asyncio.Semaphore | None = None  # initialised lazily in async context\n",
        )
    if 'task_name = f"motion_clip:{camera_entity_id}"' not in updated:
        updated = updated.replace(
            "        import uuid\n",
            "        task_name = f\"motion_clip:{camera_entity_id}\"\n"
            "        for t in self._tasks:\n"
            "            if t.get_name() == task_name and not t.done():\n"
            "                _LOGGER.info(\n"
            "                    \"motion_clip.capture_skipped_already_running\",\n"
            "                    camera=camera_entity_id,\n"
            "                )\n"
            "                return None\n\n"
            "        import uuid\n",
        )
        updated = updated.replace("        )\n        self._tasks.add(task)\n", "        ,\n            name=task_name,\n        )\n        self._tasks.add(task)\n")
    if "if self._capture_semaphore is None:" not in updated and "status = \"ready\"\n" in updated:
        updated = updated.replace(
            "        status = \"ready\"\n"
            "        if not await self._capture_clip(camera_entity_id, fullpath):\n"
            "            status = \"capture_failed\"\n",
            "        if self._capture_semaphore is None:\n"
            "            self._capture_semaphore = asyncio.Semaphore(2)\n\n"
            "        status = \"ready\"\n"
            "        async with self._capture_semaphore:\n"
            "            if not await self._capture_clip(camera_entity_id, fullpath):\n"
            "                status = \"capture_failed\"\n",
        )
    if updated == original:
        return None

    return KnownFixProposal(
        summary=(
            "The motion clip pipeline is showing timeout/process-pressure symptoms consistent with "
            "unbounded concurrent ffmpeg work. This deterministic fix adds per-camera task deduplication "
            "and a lazy global semaphore around capture. If the service still exhausts memory after this, "
            "raise `MemoryMax=` on `avatar-backend.service` as an operator follow-up."
        ),
        diff=_build_diff(source_file, original, updated),
    )


def _propose_polling_only_camera_fix(error: NovaError, source_file: Path) -> KnownFixProposal | None:
    if source_file.name != "motion_clip_service.py":
        return None
    if not _looks_like_motion_clip_timeout(error):
        return None

    camera_id = _extract_camera_id(error)
    if not camera_id:
        return KnownFixProposal(
            summary=(
                "A motion clip timeout was detected, but the log line did not include a camera entity id. "
                "The polling-only camera rule only patches known camera ids, so this one needs manual review."
            ),
            diff="",
        )

    original = source_file.read_text(encoding="utf-8")
    if "return await self._capture_clip_polling(camera_entity_id, output_path)" in original.split("async def _capture_clip", 1)[1].split("async def _capture_clip_polling", 1)[0]:
        return KnownFixProposal(
            summary=(
                f"{camera_id} is already effectively polling-only because `_capture_clip()` now bypasses "
                "MJPEG globally. No config patch is needed for this specific camera."
            ),
            diff="",
        )

    repo_root = _repo_root_for(source_file)
    if repo_root is None:
        return None
    runtime_path = repo_root / "config" / "home_runtime.json"
    if not runtime_path.exists():
        return KnownFixProposal(
            summary=(
                f"{camera_id} is repeatedly timing out on MJPEG capture, but `config/home_runtime.json` "
                "was not found so the deterministic polling-only rule cannot persist the camera override."
            ),
            diff="",
        )

    runtime_original = runtime_path.read_text(encoding="utf-8")
    try:
        runtime_data = json.loads(runtime_original or "{}")
    except json.JSONDecodeError:
        return KnownFixProposal(
            summary=(
                "The polling-only camera rule found a candidate timeout pattern, but `home_runtime.json` "
                "is not valid JSON right now. Manual repair is needed before self-heal can edit it."
            ),
            diff="",
        )

    polling_only = list(runtime_data.get("polling_only_cameras") or [])
    if camera_id in polling_only:
        return KnownFixProposal(
            summary=(
                f"{camera_id} is already listed in `polling_only_cameras`, so the timeout is not fixable "
                "by adding another per-camera polling override."
            ),
            diff="",
        )

    polling_only.append(camera_id)
    polling_only = sorted(dict.fromkeys(str(v) for v in polling_only))
    runtime_data["polling_only_cameras"] = polling_only
    runtime_updated = json.dumps(runtime_data, indent=2, sort_keys=True) + "\n"
    return KnownFixProposal(
        summary=(
            f"{camera_id} is dominating motion clip timeout logs, which points to an MJPEG path that "
            "does not sustain capture for that entity. This deterministic fix adds the camera to "
            "`polling_only_cameras` in `config/home_runtime.json` so clip capture skips MJPEG for that entity."
        ),
        diff=_build_diff(runtime_path, runtime_original, runtime_updated),
    )


def _propose_prompt_gap_fix(error: NovaError, source_file: Path) -> KnownFixProposal | None:
    haystack = f"{error.exc_value}\n{error.raw_json}".lower()
    if not _PROMPT_GAP_RE.search(haystack):
        return None
    if not any(hint in haystack for hint in _PROMPT_GAP_HINTS):
        return None

    repo_root = _repo_root_for(source_file)
    if repo_root is None:
        return None
    prompt_path = repo_root / "config" / "system_prompt.txt"
    if not prompt_path.exists():
        return None

    original = prompt_path.read_text(encoding="utf-8")
    reminder = (
        "\n\nHome Assistant access reminder:\n"
        "- Do not claim you cannot access a Home Assistant integration, entity, or weather data if relevant tools/entities exist.\n"
        "- Use the available Home Assistant tools first, then explain the result.\n"
        "- If access is genuinely unavailable, say which tool/entity lookup failed instead of saying access is impossible.\n"
    )
    if "Home Assistant access reminder:" in original:
        return KnownFixProposal(
            summary=(
                "The system prompt already includes a Home Assistant access reminder, so this prompt-gap "
                "report is not fixable by the deterministic prompt patch alone."
            ),
            diff="",
        )

    updated = original.rstrip() + reminder
    return KnownFixProposal(
        summary=(
            "Nova reported that it could not access an integration that should be reachable through Home Assistant. "
            "This deterministic fix patches `config/system_prompt.txt` so the assistant is instructed to try the "
            "Home Assistant tools/entities before claiming that access is unavailable."
        ),
        diff=_build_diff(prompt_path, original, updated),
    )


def _looks_like_gemini_429(error: NovaError) -> bool:
    haystack = f"{error.event}\n{error.exc_type}\n{error.exc_value}\n{error.raw_json}".lower()
    if "429" not in haystack and "rate limit" not in haystack:
        return False
    return any(marker in haystack for marker in ("gemini", "generativelanguage.googleapis.com", "describe_image"))


def _looks_like_motion_clip_pressure(error: NovaError) -> bool:
    haystack = f"{error.event}\n{error.exc_type}\n{error.exc_value}\n{error.raw_json}".lower()
    return any(marker in haystack for marker in ("motion_clip.capture_timeout", "motion_clip.poll_encode_timeout", "processlookuperror"))


def _looks_like_motion_clip_timeout(error: NovaError) -> bool:
    haystack = f"{error.event}\n{error.exc_type}\n{error.exc_value}\n{error.raw_json}".lower()
    return any(marker in haystack for marker in ("motion_clip.capture_timeout", "mjpeg stream timed out", "capture_fallback_to_polling"))


def _extract_camera_id(error: NovaError) -> str:
    try:
        payload = json.loads(error.raw_json)
        camera = str(payload.get("camera") or "").strip()
        if camera:
            return camera
    except Exception:
        pass
    match = re.search(r"camera(?:_entity_id)?[=:]\s*['\"]?([A-Za-z0-9_.]+)", error.raw_json)
    return match.group(1) if match else ""


def _safe_parse(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _class_assigns_attribute(class_node: ast.ClassDef, attr_name: str) -> bool:
    for node in ast.walk(class_node):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == attr_name
            ):
                return True
    return False


def _replace_method(source: str, class_name: str, method_name: str, replacement: str) -> str:
    module = _safe_parse(source)
    if module is None:
        return source
    lines = source.splitlines(keepends=True)
    replacement_lines = replacement.splitlines(keepends=True)
    if replacement_lines and not replacement_lines[-1].endswith("\n"):
        replacement_lines[-1] += "\n"
    for node in module.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in node.body:
            if isinstance(child, ast.AsyncFunctionDef) and child.name == method_name:
                start = child.lineno - 1
                end = child.end_lineno
                new_lines = lines[:start] + replacement_lines + lines[end:]
                return "".join(new_lines)
    return source


def _build_diff(path: Path, original: str, updated: str) -> str:
    repo_root = _repo_root_for(path)
    rel_path = str(path.relative_to(repo_root)) if repo_root is not None else path.name
    return "".join(
        unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )


def _repo_root_for(path: Path) -> Path | None:
    for parent in (path,) + tuple(path.parents):
        if (parent / ".git").exists():
            return parent
    return None
