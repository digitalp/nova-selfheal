from __future__ import annotations

from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path

from nova_selfheal.models import NovaError

_WEBSOCKET_CONTAINER_EVENT = "websocket.container_dependency_error"
_WEBSOCKET_CONTAINER_LOGGER = "avatar_backend.bootstrap.container"
_WEBSOCKET_CONTAINER_MARKER = "get_container() missing 1 required positional argument"


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
    if error.event != _WEBSOCKET_CONTAINER_EVENT:
        return None
    if source_file.name != "container.py":
        return None

    original = source_file.read_text(encoding="utf-8")
    if "from starlette.requests import HTTPConnection" in original and "def get_container(connection: HTTPConnection)" in original:
        return KnownFixProposal(
            summary=(
                "The websocket routes are using Depends(get_container), but the helper was typed "
                "for Request-only injection. FastAPI resolves websocket dependencies with an "
                "HTTPConnection-compatible object, so websocket connection setup fails before the "
                "route body runs. The fix is to accept HTTPConnection in get_container() so the "
                "same dependency works for both HTTP and websocket routes."
            ),
            diff="",
            verification_mode="websocket_container",
        )

    updated = original
    updated = updated.replace("from fastapi import Request", "from starlette.requests import HTTPConnection")
    updated = updated.replace(
        'def get_container(request: Request) -> "AppContainer":\n    """FastAPI Depends() — extract the typed AppContainer from app.state."""\n    return request.app.state._container\n',
        'def get_container(connection: HTTPConnection) -> "AppContainer":\n    """FastAPI Depends() — extract the typed AppContainer from app.state.\n\n    Accepts both HTTP requests and websocket connections.\n    """\n    return connection.app.state._container\n',
    )

    if updated == original:
        return None

    diff = "".join(
        unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile="a/avatar_backend/bootstrap/container.py",
            tofile="b/avatar_backend/bootstrap/container.py",
        )
    )
    return KnownFixProposal(
        summary=(
            "The websocket routes are using Depends(get_container), but the helper was typed "
            "for Request-only injection. FastAPI resolves websocket dependencies with an "
            "HTTPConnection-compatible object, so websocket connection setup fails before the "
            "route body runs. The fix is to accept HTTPConnection in get_container() so the "
            "same dependency works for both HTTP and websocket routes."
        ),
        diff=diff,
        verification_mode="websocket_container",
    )
