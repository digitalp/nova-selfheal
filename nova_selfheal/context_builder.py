from __future__ import annotations
from pathlib import Path
import re
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from nova_selfheal.models import NovaError

_LOGGER = structlog.get_logger(__name__)


class ContextBuilder:
    """
    Maps a structlog logger name (e.g. 'avatar_backend.services.ha_proxy')
    to the corresponding source file under nova_path/avatar_backend/.

    Safety invariant: resolved path MUST be inside nova_path/avatar_backend/.
    Anything outside is silently rejected (returns None).
    """

    def __init__(self, nova_path: Path) -> None:
        self._nova_path = nova_path
        self._allowed_root = (nova_path / "avatar_backend").resolve()

    def resolve_source_file(self, logger_name: str) -> Path | None:
        """Convert 'avatar_backend.services.foo' → /opt/avatar-server/avatar_backend/services/foo.py"""
        if not logger_name:
            return None

        # Convert dots to slashes, append .py
        rel = logger_name.replace(".", "/") + ".py"
        candidate = (self._nova_path / rel).resolve()

        if not self._is_safe(candidate):
            # Try stripping a leading component (handles double-prefix cases)
            parts = logger_name.split(".")
            if len(parts) > 1:
                rel2 = "/".join(parts[1:]) + ".py"
                candidate2 = (self._nova_path / "avatar_backend" / rel2).resolve()
                if self._is_safe(candidate2) and candidate2.exists():
                    return candidate2
            _LOGGER.warning("context_builder.path_rejected", logger=logger_name, path=str(candidate))
            return None

        if not candidate.exists():
            _LOGGER.warning("context_builder.file_not_found", path=str(candidate))
            return None

        return candidate

    def resolve_source(self, error: "NovaError") -> Path | None:
        """Resolve source from logger first, then fall back to traceback file paths."""
        source = self.resolve_source_file(error.logger)
        if source is not None:
            return source

        for match in re.finditer(r'File "([^"]+)"', error.raw_json):
            candidate = Path(match.group(1)).resolve()
            if self._is_safe(candidate) and candidate.exists():
                return candidate
        return None

    def _is_safe(self, path: Path) -> bool:
        """Return True iff path is inside the allowed avatar_backend root."""
        try:
            path.relative_to(self._allowed_root)
            return True
        except ValueError:
            return False

    def build_prompt_context(self, error: "NovaError", source_file: Path, aggregate_stats: str = "") -> str:
        """Read source file and return a formatted context string for the Claude prompt."""
        try:
            source_content = source_file.read_text(encoding="utf-8")
        except OSError as exc:
            _LOGGER.warning("context_builder.read_failed", path=str(source_file), exc=repr(exc))
            source_content = "# (could not read file)"

        rel_path = source_file.relative_to(self._nova_path)

        ctx = (
            f"ERROR DETAILS:\n"
            f"- Event:     {error.event}\n"
            f"- Exception: {error.exc_type}: {error.exc_value}\n"
            f"- Logger:    {error.logger}\n"
            f"- Timestamp: {error.timestamp}\n"
            f"- Service:   {error.service}\n"
        )
        if aggregate_stats:
            ctx += f"\n{aggregate_stats}\n"
        ctx += (
            f"\nSOURCE FILE: {rel_path}\n"
            f"```python\n{source_content}\n```"
        )
        return ctx
