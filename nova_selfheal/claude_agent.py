from __future__ import annotations
import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from nova_selfheal.config import Settings
    from nova_selfheal.models import NovaError

_LOGGER = structlog.get_logger(__name__)

_PROMPT_TEMPLATE = """\
You are a Python debugging assistant for Nova, an AI home assistant backend.

A Nova service has logged an ERROR. Your job:
1. Analyse the error and the source file below
2. Propose a minimal fix as a unified diff
3. Write a one-paragraph plain-English summary of the root cause and fix

{error_context}
"""


class ClaudeAgent:
    """
    Invokes the Claude Code CLI in non-interactive (--print) mode.
    Passes the prompt via stdin; reads the response from stdout.
    Claude reads CLAUDE.md from the working directory automatically.
    """

    def __init__(self, work_dir: Path, settings: "Settings") -> None:
        self._work_dir = work_dir
        self._timeout = settings.claude_timeout_seconds

    async def generate_fix(self, error: "NovaError", error_context: str) -> str:
        """
        Invoke Claude and return its raw text output.
        Raises RuntimeError if Claude is not found or times out.
        """
        prompt = _PROMPT_TEMPLATE.format(error_context=error_context)

        cmd = [
            "claude",
            "--print",
            "--allowedTools", "Read",
            "--output-format", "text",
        ]

        _LOGGER.info("claude_agent.invoking", event=error.event, timeout=self._timeout)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._work_dir),
                env={**os.environ},
            )
        except FileNotFoundError:
            raise RuntimeError(
                "claude CLI not found — run install.sh to install it"
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=float(self._timeout),
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            raise RuntimeError(f"Claude timed out after {self._timeout}s")

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", "replace")[:500]
            _LOGGER.warning("claude_agent.nonzero_exit",
                            returncode=proc.returncode, stderr=err_text)

        output = stdout.decode("utf-8", "replace").strip()
        _LOGGER.info("claude_agent.done", chars=len(output), event=error.event)
        return output
