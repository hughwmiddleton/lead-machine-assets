"""Lightweight round-robin scheduler for interleaving source work.

This module is intentionally dependency-free so it can be unit-tested in
isolation. It cycles through sources in order, advancing each source's row
cursor one step per round to avoid long single-source bursts.
"""

from __future__ import annotations

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
        if result.skipped_cooldown:
            summary["skipped_cooldown"] += 1
        if result.attempted:
            summary["attempted"] += 1
        if result.enriched:
            summary["enriched"] += 1

    def run(self) -> Dict[str, Dict[str, int]]:
        """Run all sources in a round-robin fashion and return counters."""

        if not self.sources:
            return {}
        while True:
            progressed = False
            for spec in self.sources:
                row_idx = self._next_row(spec)
                if row_idx is None:
                    continue
                progressed = True
                available, reason = spec.is_available()
                display_row = self._row_label(row_idx)
                if not available:
                    if reason == "cooldown":
                        self._summary[spec.name]["skipped_cooldown"] += 1
                        self._emit(f"[Scheduler] skipping {spec.name} (cooldown) row {display_row}")
                    continue
                self._emit(f"[Scheduler] running {spec.name} for row {display_row}")
                result = spec.run_row(row_idx)
                self._record(spec.name, result)
            if not progressed:
                break
        return self._summary

