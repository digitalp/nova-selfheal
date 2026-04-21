"""
LogAggregator — tracks recent errors/warnings in a sliding window and
provides aggregate stats to enrich the Claude prompt.

Instead of Claude seeing "one capture_timeout error", it sees:
  "capture_timeout has occurred 27 times in the last hour,
   96% from camera.tangu_home_door_bell, polling fallback succeeds 77%"

This gives Claude the same data a human would gather before deciding on a fix.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nova_selfheal.models import NovaError

_WINDOW_S = 3600  # 1 hour sliding window
_ESCALATION_THRESHOLD = 10  # warnings repeated this many times become actionable


@dataclass
class AggregateStats:
    event: str
    total_count: int
    window_s: int
    by_entity: dict[str, int]
    by_level: dict[str, int]
    related_events: dict[str, int]  # other events from same logger in window
    first_seen: float
    last_seen: float

    def format_for_prompt(self) -> str:
        lines = [
            f"AGGREGATE LOG ANALYSIS (last {self.window_s // 60} min):",
            f"- This event ({self.event}) has occurred {self.total_count} time(s)",
        ]
        if self.by_entity:
            top = sorted(self.by_entity.items(), key=lambda x: -x[1])[:5]
            lines.append("- Breakdown by entity/camera:")
            for entity, count in top:
                pct = count * 100 // self.total_count
                lines.append(f"    {entity}: {count} ({pct}%)")
        if self.by_level:
            lines.append(f"- By level: {dict(self.by_level)}")
        if self.related_events:
            top_related = sorted(self.related_events.items(), key=lambda x: -x[1])[:5]
            lines.append("- Related events from same logger:")
            for evt, count in top_related:
                lines.append(f"    {evt}: {count}")
        elapsed = self.last_seen - self.first_seen
        if elapsed > 0 and self.total_count > 1:
            rate = self.total_count / (elapsed / 60)
            lines.append(f"- Rate: ~{rate:.1f}/min over {elapsed / 60:.0f} min")
        return "\n".join(lines)


class LogAggregator:
    """Sliding-window tracker for all log events (errors + warnings)."""

    def __init__(self, window_s: int = _WINDOW_S) -> None:
        self._window_s = window_s
        # event_name → list of (timestamp, raw_json_str)
        self._entries: defaultdict[str, list[tuple[float, str]]] = defaultdict(list)
        # logger → event → count (for related event lookup)
        self._by_logger: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))

    def record(self, error: "NovaError") -> None:
        now = time.monotonic()
        self._entries[error.event].append((now, error.raw_json))
        self._by_logger[error.logger][error.event] += 1
        self._prune()

    def get_stats(self, error: "NovaError") -> AggregateStats:
        self._prune()
        entries = self._entries.get(error.event, [])

        by_entity: defaultdict[str, int] = defaultdict(int)
        by_level: defaultdict[str, int] = defaultdict(int)
        first_seen = 0.0
        last_seen = 0.0

        for ts, raw in entries:
            if first_seen == 0.0:
                first_seen = ts
            last_seen = ts
            try:
                inner = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            by_level[inner.get("level", "unknown")] += 1
            # Extract entity from common fields
            for key in ("camera", "entity_id", "entity", "camera_entity_id", "model", "provider", "service"):
                val = inner.get(key)
                if val:
                    by_entity[str(val)] += 1
                    break

        # Related events from the same logger
        related = dict(self._by_logger.get(error.logger, {}))
        related.pop(error.event, None)

        return AggregateStats(
            event=error.event,
            total_count=len(entries),
            window_s=self._window_s,
            by_entity=dict(by_entity),
            by_level=dict(by_level),
            related_events=related,
            first_seen=first_seen,
            last_seen=last_seen,
        )

    def should_escalate_warning(self, error: "NovaError") -> bool:
        """Return True if a warning has repeated enough times to be worth investigating."""
        if error.level != "warning":
            return False
        self._prune()
        return len(self._entries.get(error.event, [])) >= _ESCALATION_THRESHOLD

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._window_s
        for event in list(self._entries):
            self._entries[event] = [(t, r) for t, r in self._entries[event] if t > cutoff]
            if not self._entries[event]:
                del self._entries[event]
