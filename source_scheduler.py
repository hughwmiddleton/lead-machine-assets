"""Lightweight round-robin scheduler for interleaving source work.

This module is intentionally dependency-free so it can be unit-tested in
isolation. It cycles through sources in order, advancing each source's row
cursor one step per round to avoid long single-source bursts.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


def _row_source_opportunity(row: Any, source_name: str) -> bool:
    """Return True when a source has a realistic opportunity to enrich the row.

    The checks are deliberately defensive: missing keys or unexpected row
    objects should not raise. When we cannot inspect the row, we allow the
    attempt so existing behaviour is preserved.
    """

    def _get_value(keys: Sequence[str]) -> Any:
        if row is None:
            return None
        for key in keys:
            try:
                if isinstance(row, dict):
                    if key in row:
                        return row.get(key)
                    continue
                if hasattr(row, "get"):
                    # pandas Series provides .get
                    val = row.get(key, None)  # type: ignore[arg-type]
                    if val is not None:
                        return val
                    continue
                if hasattr(row, key):
                    return getattr(row, key)
            except Exception:
                continue
        return None

    def _has_text(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        return bool(value)

    name = (source_name or "").strip().upper()
    artist_present = _has_text(_get_value(["artist", "Artist Name", "artist_name"]))

    if name in {"SC", "SOUNDCLOUD"}:
        sc_url = _get_value(
            ["soundcloud_url", "SoundCloud Link", "Soundcloud Link", "soundcloud", "SC_URL"]
        )
        return artist_present and not _has_text(sc_url)

    if name in {"LF", "LASTFM", "LAST.FM"}:
        lf_url = _get_value(["lastfm_url", "LastFM URL", "Last FM URL", "LastFM Link", "lastfm"])
        return artist_present and not _has_text(lf_url)

    if name in {"FB", "FACEBOOK"}:
        fb_url = _get_value(["facebook_url", "Facebook_URL", "Facebook URL", "FB_URL", "facebook"])
        if _has_text(fb_url):
            return True

        social_text = _get_value([
            "Social Link",
            "social_link",
            "social",
            "links",
            "External Links",
            "external_links",
        ])
        if _has_text(social_text):
            try:
                text = str(social_text).lower()
                if any(domain in text for domain in ("facebook.com", "m.facebook.com", "fb.com")):
                    return True
            except Exception:
                pass

        return False

    # Unknown sources fall back to existing behaviour.
    return True


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
    row_getter: Optional[Callable[[int], Any]] = None


class SourceDiversityScheduler:
    """Round-robin scheduler that interleaves sources across rows."""

    def __init__(
        self,
        sources: Sequence[SourceSpec],
        row_label: Optional[Callable[[int], str]] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        short_circuit_fn: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        self.sources: List[SourceSpec] = list(sources)
        self._positions: Dict[str, int] = {spec.name: 0 for spec in self.sources}
        self._row_label = row_label or (lambda idx: str(idx))
        self._log = log_fn
        self._short_circuit_fn = short_circuit_fn
        self._completed_rows: set[int] = set()
        self._summary: Dict[str, Dict[str, int]] = {
            spec.name: {"attempted": 0, "enriched": 0, "skipped_cooldown": 0, "skipped_opportunity": 0}
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

    def _peek_next_row(self, spec: SourceSpec) -> Optional[int]:
        pos = self._positions.get(spec.name, 0)
        rows = spec.rows
        while pos < len(rows):
            candidate = rows[pos]
            if candidate not in self._completed_rows:
                return candidate
            pos += 1
        return None

    def _next_row(self, spec: SourceSpec) -> Optional[int]:
        pos = self._positions.get(spec.name, 0)
        rows = spec.rows
        while pos < len(rows):
            row_idx = rows[pos]
            pos += 1
            self._positions[spec.name] = pos
            if row_idx in self._completed_rows:
                continue
            return row_idx
        return None

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
        base_score = max(0.2, min(3.0, priority))

        # Opportunity-weighted multiplier based on the next pending row for this source.
        opportunity_weight = 1.0
        next_row = self._peek_next_row(spec)
        if next_row is not None and spec.row_getter:
            try:
                row_data = spec.row_getter(next_row)
                if row_data is None:
                    opportunity_weight = 1.0
                else:
                    has_opportunity = _row_source_opportunity(row_data, spec.name)
                    opportunity_weight = 1.5 if has_opportunity else 0.0
            except Exception:
                opportunity_weight = 1.0
        return base_score * opportunity_weight

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
                    key=lambda s: (self._compute_priority(s), s.name),
                    reverse=True,
                )
            except Exception:
                sorted_sources = self.sources

            for spec in sorted_sources:
                row_idx = self._next_row(spec)
                if row_idx is None:
                    continue
                progressed = True
                row_data = None
                if spec.row_getter:
                    try:
                        row_data = spec.row_getter(row_idx)
                    except Exception:
                        row_data = None
                has_opportunity = True
                if row_data is not None:
                    try:
                        has_opportunity = _row_source_opportunity(row_data, spec.name)
                    except Exception:
                        has_opportunity = True
                if not has_opportunity:
                    self._summary[spec.name]["skipped_opportunity"] += 1
                    display_row = self._row_label(row_idx)
                    self._emit(f"[Scheduler] skipping {spec.name} row {display_row} (no opportunity)")
                    continue
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
                if result.attempted and self._short_circuit_fn:
                    try:
                        latest_row = spec.row_getter(row_idx) if spec.row_getter else None
                        if latest_row is not None and self._short_circuit_fn(latest_row):
                            self._completed_rows.add(row_idx)
                            self._emit(f"[Scheduler] row {display_row} email found; short-circuiting remaining sources")
                    except Exception:
                        pass
            if not progressed:
                break
        return self._summary
