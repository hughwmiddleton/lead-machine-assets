"""Lightweight round-robin scheduler for interleaving source work.

This module is intentionally dependency-free so it can be unit-tested in
isolation. It cycles through sources in order, advancing each source's row
cursor one step per round to avoid long single-source bursts.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple


@dataclass
class SourceResult:
    """Outcome for a single source/row attempt."""

    attempted: bool = False
    enriched: bool = False
    skipped_cooldown: bool = False


@dataclass
class SourceSpec:
    """Description of a source scheduled for interleaving."""

    name: str
    rows: Sequence[int]
    run_row: Callable[[int], SourceResult]
    is_available: Callable[[], Tuple[bool, Optional[str]]]


class SourceDiversityScheduler:
    """Round-robin scheduler that interleaves sources across rows."""

    def __init__(
        self,
        sources: Sequence[SourceSpec],
        row_label: Optional[Callable[[int], str]] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.sources: List[SourceSpec] = list(sources)
        self._positions: Dict[str, int] = {spec.name: 0 for spec in self.sources}
        self._row_label = row_label or (lambda idx: str(idx))
        self._log = log_fn
        self._summary: Dict[str, Dict[str, int]] = {
            spec.name: {"attempted": 0, "enriched": 0, "skipped_cooldown": 0}
            for spec in self.sources
        }
        self._metrics: Dict[str, Dict[str, object]] = {
            spec.name: {
                "attempts": 0,
                "enriched": 0,
                "cooldown": 0,
                "errors": 0,
                "recent_enrichments": deque(maxlen=20),
            }
            for spec in self.sources
        }
        self._iteration_counter: int = 0
        self._health_log_every: int = 50

    def _emit(self, message: str) -> None:
        if self._log:
            try:
                self._log(message)
            except Exception:
                pass

    def _next_row(self, spec: SourceSpec) -> Optional[int]:
        pos = self._positions.get(spec.name, 0)
        if pos >= len(spec.rows):
            return None
        row_idx = spec.rows[pos]
        self._positions[spec.name] = pos + 1
        return row_idx

    def _record(self, spec_name: str, result: SourceResult) -> None:
        summary = self._summary[spec_name]
        metrics = self._metrics[spec_name]

        if result.skipped_cooldown:
            summary["skipped_cooldown"] += 1
            metrics["cooldown"] += 1

        if result.attempted:
            summary["attempted"] += 1
            metrics["attempts"] += 1
            metrics["recent_enrichments"].append(bool(result.enriched))
            if result.enriched:
                summary["enriched"] += 1
                metrics["enriched"] += 1
            else:
                metrics["errors"] += 1

        self._maybe_emit_health()

    def _record_cooldown(self, spec_name: str) -> None:
        self._summary[spec_name]["skipped_cooldown"] += 1
        metrics = self._metrics[spec_name]
        metrics["cooldown"] += 1
        metrics["recent_enrichments"].append(False)
        self._maybe_emit_health()

    def _compute_priority(self, spec: SourceSpec) -> float:
        metrics = self._metrics.get(spec.name, {})
        attempts = int(metrics.get("attempts", 0))
        cooldown = int(metrics.get("cooldown", 0))
        enriched = int(metrics.get("enriched", 0))

        # Keep first round deterministic; add jitter once we have activity.
        jitter = random.uniform(-0.1, 0.1) if (attempts or cooldown) else 0.0

        denom = attempts or 1
        success_rate = enriched / denom if denom else 0.0
        cooldown_rate = cooldown / denom if denom else 0.0

        priority = (success_rate * 2.0) - (cooldown_rate * 1.5) + jitter
        return max(0.2, min(3.0, priority))

    def _maybe_emit_health(self) -> None:
        self._iteration_counter += 1
        if self._iteration_counter % self._health_log_every:
            return

        for name, metrics in self._metrics.items():
            attempts = int(metrics["attempts"])
            cooldown = int(metrics["cooldown"])
            enriched = int(metrics["enriched"])
            success_rate = enriched / attempts if attempts else 0.0
            cooldown_rate = cooldown / attempts if attempts else 0.0
            self._emit(
                f"[Scheduler][Health] {name} success={success_rate:.2f} cooldown={cooldown_rate:.2f} attempts={attempts}"
            )

    def run(self) -> Dict[str, Dict[str, int]]:
        """Run all sources in a round-robin fashion and return counters."""

        if not self.sources:
            return {}
        while True:
            progressed = False
            try:
                sorted_sources = sorted(
                    self.sources,
                    key=lambda s: self._compute_priority(s),
                    reverse=True,
                )
            except Exception:
                sorted_sources = self.sources

            for spec in sorted_sources:
                row_idx = self._next_row(spec)
                if row_idx is None:
                    continue
                progressed = True
                available, reason = spec.is_available()
                display_row = self._row_label(row_idx)
                if not available:
                    if reason == "cooldown":
                        self._record_cooldown(spec.name)
                        self._emit(f"[Scheduler] skipping {spec.name} (cooldown) row {display_row}")
                    continue
                self._emit(f"[Scheduler] running {spec.name} for row {display_row}")
                result = spec.run_row(row_idx)
                self._record(spec.name, result)
            if not progressed:
                break
        return self._summary
