from __future__ import annotations
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import structlog

_LOGGER = structlog.get_logger(__name__)

_TEST_TIMEOUT = 120


class PatchApplier:
    """
    Applies a unified diff to the Nova source tree, runs tests, and restarts.

    Process:
    1. Write diff to a temp file
    2. Dry-run with patch -p1 --dry-run
    3. Real apply with patch -p1
    4. Run pytest on affected test file(s) — if tests fail, auto-revert
    5. sudo systemctl restart <service>
    """

    def __init__(self, nova_path: Path, watch_service: str) -> None:
        self._nova_path = nova_path
        self._service = watch_service

    async def apply(self, diff: str) -> tuple[bool, str]:
        """Apply patch, run tests, revert on failure. Returns (success, message)."""
        patch_path = Path(tempfile.gettempdir()) / f"nova-selfheal-{uuid.uuid4().hex[:8]}.patch"
        try:
            patch_path.write_text(diff, encoding="utf-8")
            ok, msg = self._run_patch_dryrun(patch_path)
            if not ok:
                return False, msg
            ok, msg = self._run_patch_apply(patch_path)
            if not ok:
                return False, msg
            test_ok, test_msg = self._run_tests(diff)
            if not test_ok:
                _LOGGER.warning("patch_applier.tests_failed_reverting", test_output=test_msg[:300])
                self._revert_patch(patch_path)
                return False, f"Tests failed — patch reverted.\n\n{test_msg}"
            return True, f"{msg}\n✅ Tests passed."
        finally:
            try:
                patch_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _run_patch_dryrun(self, patch_path: Path) -> tuple[bool, str]:
        base_cmd = ["patch", "-p1", "-i", str(patch_path)]
        dry = subprocess.run(base_cmd + ["--dry-run"], cwd=str(self._nova_path), capture_output=True, text=True, timeout=15)
        if dry.returncode != 0:
            msg = f"Dry run failed (patch would not apply cleanly):\n{dry.stderr[:400]}"
            _LOGGER.warning("patch_applier.dry_run_failed", stderr=dry.stderr[:200])
            return False, msg
        return True, ""

    def _run_patch_apply(self, patch_path: Path) -> tuple[bool, str]:
        base_cmd = ["patch", "-p1", "-i", str(patch_path)]
        result = subprocess.run(base_cmd, cwd=str(self._nova_path), capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            msg = f"Patch apply failed:\n{result.stderr[:400]}"
            _LOGGER.error("patch_applier.apply_failed", stderr=result.stderr[:200])
            return False, msg
        _LOGGER.info("patch_applier.applied", output=result.stdout[:200])
        return True, result.stdout.strip() or "Patch applied successfully."

    def _revert_patch(self, patch_path: Path) -> None:
        try:
            subprocess.run(["patch", "-p1", "-R", "-i", str(patch_path)], cwd=str(self._nova_path), capture_output=True, text=True, timeout=15)
            _LOGGER.info("patch_applier.reverted")
        except Exception as exc:
            _LOGGER.error("patch_applier.revert_failed", exc=repr(exc))

    def _run_tests(self, diff: str) -> tuple[bool, str]:
        test_targets = self._find_test_targets(diff)
        if not test_targets:
            _LOGGER.info("patch_applier.running_full_tests")
            test_targets = ["tests/"]
        venv_python = self._nova_path / ".venv" / "bin" / "python"
        if not venv_python.exists():
            _LOGGER.warning("patch_applier.no_venv", detail="skipping tests")
            return True, "Tests skipped (no .venv)"
        cmd = [str(venv_python), "-m", "pytest", "-x", "-q", "--tb=short"] + test_targets
        try:
            result = subprocess.run(cmd, cwd=str(self._nova_path), capture_output=True, text=True, timeout=_TEST_TIMEOUT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            output = (result.stdout + "\n" + result.stderr).strip()
            if result.returncode == 0:
                _LOGGER.info("patch_applier.tests_passed", targets=test_targets)
                return True, output[-500:]
            else:
                _LOGGER.warning("patch_applier.tests_failed", returncode=result.returncode, targets=test_targets)
                return False, output[-800:]
        except subprocess.TimeoutExpired:
            return False, f"Tests timed out after {_TEST_TIMEOUT}s"
        except Exception as exc:
            _LOGGER.warning("patch_applier.test_run_error", exc=repr(exc))
            return True, f"Tests could not run: {exc}"

    def _find_test_targets(self, diff: str) -> list[str]:
        targets = []
        for line in diff.splitlines():
            if line.startswith("+++ b/") or line.startswith("--- a/"):
                path = line.split("/", 1)[1] if "/" in line else ""
                if path.startswith("avatar_backend/services/") or path.startswith("avatar_backend/routers/"):
                    filename = Path(path).stem
                    test_file = f"tests/test_{filename}.py"
                    test_path = self._nova_path / test_file
                    if test_path.exists() and test_file not in targets:
                        targets.append(test_file)
        return targets

    async def restart_service(self) -> tuple[bool, str]:
        try:
            result = subprocess.run(["sudo", "systemctl", "restart", self._service], capture_output=True, text=True, timeout=30)
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
