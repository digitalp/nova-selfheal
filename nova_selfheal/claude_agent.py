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
    Falls back to OpenAI API if Claude fails (quota exhausted, auth error, etc.).
    """

    def __init__(self, work_dir: Path, settings: "Settings") -> None:
        self._work_dir = work_dir
        self._timeout = settings.claude_timeout_seconds
        self._openai_api_key = settings.openai_api_key
        self._openai_model = settings.openai_model

    async def generate_fix(self, error: "NovaError", error_context: str) -> str:
        """
        Invoke Claude and return its raw text output.
        Falls back to OpenAI if Claude is unavailable.
        Raises RuntimeError if all providers fail.
        """
        prompt = _PROMPT_TEMPLATE.format(error_context=error_context)

        # ── Try Claude CLI first ──────────────────────────────────────────────
        try:
            output = await self._call_claude(error, prompt)
            if output:
                return output
            _LOGGER.warning("claude_agent.empty_output", log_event=error.event)
        except RuntimeError as exc:
            _LOGGER.warning("claude_agent.claude_failed", exc=repr(exc), log_event=error.event)

        # ── Fallback to OpenAI ────────────────────────────────────────────────
        if self._openai_api_key:
            _LOGGER.info("claude_agent.openai_fallback", log_event=error.event, model=self._openai_model)
            try:
                output = await self._call_openai(error, prompt)
                if output:
                    return output
            except Exception as exc:
                _LOGGER.error("claude_agent.openai_failed", exc=repr(exc))
                raise RuntimeError(f"Both Claude and OpenAI failed. Last error: {exc}")
        else:
            raise RuntimeError(
                "Claude CLI failed and no OPENAI_API_KEY configured as fallback"
            )

        raise RuntimeError("All providers returned empty output")

    async def _call_claude(self, error: "NovaError", prompt: str) -> str:
        cmd = [
            "claude",
            "--print",
            "--allowedTools", "Read",
            "--output-format", "text",
        ]

        _LOGGER.info("claude_agent.invoking", log_event=error.event, timeout=self._timeout)

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
            raise RuntimeError("claude CLI not found — run install.sh to install it")

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

        output = stdout.decode("utf-8", "replace").strip()

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", "replace")[:300]
            _LOGGER.warning("claude_agent.nonzero_exit",
                            returncode=proc.returncode, stderr=err_text,
                            output_preview=output[:200])
            # Treat non-zero exit as failure so fallback can trigger
            raise RuntimeError(
                f"claude exited {proc.returncode}: {err_text or output[:200]}"
            )

        _LOGGER.info("claude_agent.done", chars=len(output), log_event=error.event, provider="claude")
        return output

    async def _call_openai(self, error: "NovaError", prompt: str) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self._openai_api_key)

        # Read CLAUDE.md from work dir for context, same as Claude CLI would
        claude_md = ""
        claude_md_path = self._work_dir / "CLAUDE.md"
        if claude_md_path.exists():
            try:
                claude_md = claude_md_path.read_text(encoding="utf-8")[:4000]
            except Exception:
                pass

        system_msg = "You are a Python debugging assistant for Nova, an AI home assistant backend."
        if claude_md:
            system_msg += f"\n\nProject context (CLAUDE.md):\n{claude_md}"

        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=self._openai_model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=2048,
            ),
            timeout=float(self._timeout),
        )

        output = (response.choices[0].message.content or "").strip()
        _LOGGER.info("claude_agent.done", chars=len(output), log_event=error.event, provider="openai")
        return output
