from __future__ import annotations
import re
from pathlib import Path

import structlog

_LOGGER = structlog.get_logger(__name__)

_SUMMARY_RE = re.compile(
    r"SUMMARY:\s*\n(.*?)(?=\nDIFF:|\Z)", re.DOTALL | re.IGNORECASE
)
_DIFF_RE = re.compile(
    r"DIFF:\s*\n```(?:diff)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE
)


def extract_summary(claude_output: str) -> str:
    m = _SUMMARY_RE.search(claude_output)
    if m:
        return m.group(1).strip()
    # Fallback: return first 500 chars if no SUMMARY section found
    return claude_output.strip()[:500]


def extract_diff(claude_output: str) -> str | None:
    m = _DIFF_RE.search(claude_output)
    if not m:
        return None
    return m.group(1).strip()


def validate_diff(diff: str, nova_path: Path) -> bool:
    """
    Safety check: every file path referenced in the diff must resolve to
    a file inside nova_path/avatar_backend/. Returns False if:
    - No --- / +++ header lines found
    - Any referenced path escapes avatar_backend/ (path traversal)
    - The same file is not used for both --- and +++ (cross-file patch)
    """
    allowed_root = (nova_path / "avatar_backend").resolve()
    lines = diff.splitlines()

    minus_path: str | None = None
    plus_path: str | None = None
    has_hunk = False

    for line in lines:
        if line.startswith("--- "):
            # Strip "--- a/" or "--- "
            raw = line[4:]
            if raw.startswith("a/"):
                raw = raw[2:]
            minus_path = raw.strip()
        elif line.startswith("+++ "):
            raw = line[4:]
            if raw.startswith("b/"):
                raw = raw[2:]
            plus_path = raw.strip()
        elif line.startswith("@@"):
            has_hunk = True

    if not minus_path or not plus_path or not has_hunk:
        _LOGGER.warning("patch_extractor.invalid_diff_structure",
                        has_minus=bool(minus_path), has_plus=bool(plus_path), has_hunk=has_hunk)
        return False

    # Both sides must reference the same file
    if minus_path != plus_path:
        _LOGGER.warning("patch_extractor.cross_file_patch",
                        minus=minus_path, plus=plus_path)
        return False

    # Resolve and check safety
    try:
        resolved = (nova_path / minus_path).resolve()
        resolved.relative_to(allowed_root)
    except (ValueError, OSError):
        _LOGGER.warning("patch_extractor.path_escape", path=minus_path)
        return False

    return True
