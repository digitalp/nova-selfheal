from __future__ import annotations
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import structlog

_LOGGER = structlog.get_logger(__name__)


class PatchApplier:
    """
    Applies a unified diff to the Nova source tree and restarts the service.

    Process:
    1. Write diff to a temp file
    2. Dry-run with `patch -p1 --dry-run` — abort if it would fail
    3. Real apply with `patch -p1`
    4. `sudo systemctl restart <service>`
    """

    def __init__(self, nova_path: Path, watch_service: str) -> None:
        self._nova_path = nova_path
        self._service = watch_service

    async def apply(self, diff: str) -> tuple[bool, str]:
        """Returns (success, message)."""
        patch_path = Path(tempfile.gettempdir()) / f"nova-selfheal-{uuid.uuid4().hex[:8]}.patch"
        try:
            patch_path.write_text(diff, encoding="utf-8")
            return self._run_patch(patch_path)
        finally:
            try:
                patch_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _run_patch(self, patch_path: Path) -> tuple[bool, str]:
        base_cmd = ["patch", "-p1", "-i", str(patch_path)]

        # Dry run first
        dry = subprocess.run(
            base_cmd + ["--dry-run"],
            cwd=str(self._nova_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if dry.returncode != 0:
            msg = f"Dry run failed (patch would not apply cleanly):\n{dry.stderr[:400]}"
            _LOGGER.warning("patch_applier.dry_run_failed", stderr=dry.stderr[:200])
            return False, msg

        # Real apply
        result = subprocess.run(
            base_cmd,
            cwd=str(self._nova_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            msg = f"Patch apply failed:\n{result.stderr[:400]}"
            _LOGGER.error("patch_applier.apply_failed", stderr=result.stderr[:200])
            return False, msg

        _LOGGER.info("patch_applier.applied", output=result.stdout[:200])
        return True, result.stdout.strip() or "Patch applied successfully."

    async def restart_service(self) -> tuple[bool, str]:
        """Returns (success, message)."""
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", self._service],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                _LOGGER.info("patch_applier.restarted", service=self._service)
                return True, f"{self._service} restarted successfully."
            msg = f"systemctl restart failed (exit {result.returncode}):\n{result.stderr[:300]}"
            _LOGGER.error("patch_applier.restart_failed", service=self._service, stderr=result.stderr[:200])
            return False, msg
        except subprocess.TimeoutExpired:
            return False, "systemctl restart timed out after 30s"
        except Exception as exc:
            return False, f"restart error: {repr(exc)}"
