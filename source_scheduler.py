"""Lightweight round-robin scheduler for interleaving source work.

This module is intentionally dependency-free so it can be unit-tested in
isolation. It cycles through sources in order, advancing each source's row
cursor one step per round to avoid long single-source bursts.
"""

from __future__ import annotations

import random
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, MutableMapping, Optional, Sequence, Tuple
import re
import urllib.parse

ADAPTIVE_PRIORITY_MIN_ATTEMPTS = 2
ADAPTIVE_PRIORITY_MAX_BONUS = 0.35
COOLDOWN_PRIORITY_MAX_PENALTY = 0.45
BASE_PRIORITY_SCORE = 1.0

# ---------------------------------------------------------------------------
# Facebook URL promotion helpers
# ---------------------------------------------------------------------------

_FB_ALLOWED_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "fb.com",
    "www.fb.com",
    "m.fb.com",
    "fb.me",
    "www.fb.me",
    "m.fb.me",
}
_FB_REJECT_PATH_PREFIXES = (
    "/share",
    "/sharer.php",
    "/dialog/share",
    "/plugins/",
    "/story.php",
    "/photo.php",
    "/permalink.php",
    "/events",
    "/groups",
    "/watch",
    "/reel",
)


def canonicalize_facebook_url(raw: Any) -> str:
    """Return a canonical Facebook page/profile URL or an empty string."""
    if raw is None:
        return ""
    try:
        text = str(raw or "").strip()
    except Exception:
        return ""
    return _normalize_fb_url(text)


def _normalize_fb_url(raw: str) -> str:
    """Normalize a Facebook URL to https://www.facebook.com/<path>.

    - Accept facebook.com/www/m plus fb.com/fb.me short domains
    - Remove query/fragment noise
    - Reject obvious share/plugin/watch/group/event endpoints
    - Accept profile.php?id=<numeric> only; reject other profile.php uses
    """
    if not raw:
        return ""
    candidate = raw.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if not candidate.startswith("http"):
        candidate = "https://" + candidate
    try:
        parsed = urllib.parse.urlparse(candidate)
    except Exception:
        return ""

    host = parsed.netloc.lower()
    if host not in _FB_ALLOWED_HOSTS:
        return ""
    path = parsed.path or ""
    if not path or path == "/":
        return ""
    lowered_path = path.lower()
    if lowered_path.startswith(_FB_REJECT_PATH_PREFIXES):
        return ""
    clean_query = ""
    if lowered_path == "/profile.php":
        qs = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=False)
        ids = qs.get("id", [])
        profile_id = (ids[0] or "").strip() if ids else ""
        if not profile_id.isdigit():
            return ""
        clean_query = f"id={profile_id}"

    # Strip query/fragment tracking noise (except allowed profile id query).
    clean_path = path.rstrip("/")
    path_stripped = clean_path.strip("/")
    if path_stripped:
        first_segment = path_stripped.split("/", 1)[0].lower()
        if first_segment in {"nan", "none", "null"}:
            return ""
    return urllib.parse.urlunparse(("https", "www.facebook.com", clean_path, "", clean_query, ""))


def extract_facebook_url_from_text(text: str) -> Optional[str]:
    """Return the first canonical Facebook page/profile URL found in free text."""
    if not text:
        return None
    parts = re.split(r"[|,\s]+", str(text))
    for part in parts:
        if not part:
            continue
        url = _normalize_fb_url(part)
        if url:
            return url
    return None


def _canonical_fb_candidate_from_row(row: MutableMapping[str, Any]) -> Tuple[str, str]:
    """Return the best canonical Facebook URL candidate plus its source field."""
    if row is None:
        return ("", "")

    def _get(keys: Sequence[str]) -> str:
        for key in keys:
            try:
                val = row.get(key) if isinstance(row, dict) or hasattr(row, "get") else getattr(row, key, None)  # type: ignore[attr-defined]
            except Exception:
                val = None
            if val:
                try:
                    import pandas as _pd  # type: ignore

                    if _pd.isna(val):
                        continue
                except Exception:
                    pass
                text_val = str(val).strip()
                if text_val:
                    return text_val
        return ""

    direct_fields = ("Facebook_URL", "facebook_url", "Facebook URL", "FB_URL", "facebook", "Facebook")
    for field in direct_fields:
        normalised = canonicalize_facebook_url(_get((field,)))
        if normalised:
            return (normalised, field)

    candidate_fields = ("Social Link", "External Links", "Website", "Websites", "Website URL")
    for field in candidate_fields:
        url = extract_facebook_url_from_text(_get((field,)))
        if url:
            return (url, field)

    return ("", "")


def ensure_canonical_facebook_url(
    row: MutableMapping[str, Any], *, set_row: bool = True
) -> Tuple[str, str]:
    """
    Resolve the best Facebook URL candidate from supported aliases and optionally
    mirror it into the canonical persisted sink ``Facebook_URL``.
    """
    if row is None:
        return ("", "")

    url, source = _canonical_fb_candidate_from_row(row)
    if not url:
        return ("", "")
    if not set_row:
        return (url, source)

    try:
        import pandas as _pd  # type: ignore

        if isinstance(row, _pd.Series):
            # Avoid SettingWithCopy; caller should assign via df.loc/df.at.
            return (url, source)
    except Exception:
        pass

    def _get_one(key: str) -> str:
        try:
            val = row.get(key) if isinstance(row, dict) or hasattr(row, "get") else getattr(row, key, None)  # type: ignore[attr-defined]
        except Exception:
            val = None
        try:
            import pandas as _pd  # type: ignore

            if _pd.isna(val):
                return ""
        except Exception:
            pass
        return str(val or "").strip() if val is not None else ""

    try:
        if hasattr(row, "__setitem__"):
            current_canonical = canonicalize_facebook_url(_get_one("Facebook_URL"))
            if current_canonical:
                url = current_canonical
            if _get_one("Facebook_URL") != url:
                row["Facebook_URL"] = url  # type: ignore[index]
            if not canonicalize_facebook_url(_get_one("facebook_url")):
                row["facebook_url"] = url  # type: ignore[index]
            if "Facebook URL" in row and not canonicalize_facebook_url(_get_one("Facebook URL")):  # type: ignore[operator]
                row["Facebook URL"] = url  # type: ignore[index]
    except Exception:
        pass
    return (url, source)


def promote_facebook_url(row: MutableMapping[str, Any], *, set_row: bool = True) -> Optional[str]:
    """
    Promote any Facebook link found in accepted aliases into canonical Facebook fields.

    - Reads canonical and legacy aliases first, then generic link fields.
    - Mirrors a valid value into ``Facebook_URL`` when recoverable.
    - Does not replace an existing canonical value with a different alias value.
    - set_row=False avoids mutating pandas Series slices (prevents SettingWithCopyWarning);
      callers can write via df.loc instead.
    """
    url, _ = ensure_canonical_facebook_url(row, set_row=set_row)
    return url or None


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
        if not _has_text(fb_url):
            try:
                fb_url = promote_facebook_url(row, set_row=False)
            except Exception:
                fb_url = fb_url
        usable_fb_url = canonicalize_facebook_url(fb_url)
        fb_discovery_attempted = _has_text(
            _get_value(
                [
                    "__fb_discovery_attempted_this_run",
                    "__fb_discovery_attempted",
                    "fb_discovery_attempted",
                ]
            )
        )
        email = _get_value(["Email", "Email_All", "email"])
        if _has_text(email):
            return False
        if not usable_fb_url and fb_discovery_attempted:
            return False
        return bool(_has_text(fb_url) or artist_present)

    # Unknown sources fall back to existing behaviour.
    return True


@dataclass
class SourceResult:
    """Outcome for a single source/row attempt."""

    attempted: bool = False
    enriched: bool = False
    skipped_cooldown: bool = False
    retry_later: bool = False
    timed_retry: Optional["TimedRetry"] = None


@dataclass
class TimedRetry:
    """Explicit timed retry metadata in the source's own clock domain."""

    ready_at: Any
    max_attempts: int


@dataclass
class SourceSpec:
    """Description of a source scheduled for interleaving."""

    name: str
    rows: Sequence[int]
    run_row: Callable[[int], SourceResult]
    is_available: Callable[[], Tuple[bool, Optional[str]]]
    row_getter: Optional[Callable[[int], Any]] = None
    retry_now: Optional[Callable[[], Any]] = None
    unavailable_retry: Optional[Callable[[int, Optional[str], int], Optional[TimedRetry]]] = None
    run_row_retry: Optional[Callable[[int, int], SourceResult]] = None


@dataclass
class _DeferredEntry:
    source_name: str
    row_idx: int
    retry_count: int
    ready_at: Any
    max_attempts: int
    ordinal: int


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
        self._row_source_failures: Dict[int, set[str]] = defaultdict(set)
        self._deferred: Dict[Tuple[str, int], _DeferredEntry] = {}
        self._deferred_ordinal: int = 0
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
                failed_sources = self._row_source_failures.get(candidate)
                if not failed_sources or spec.name not in failed_sources:
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
            failed_sources = self._row_source_failures.get(row_idx)
            if failed_sources and spec.name in failed_sources:
                continue
            return row_idx
        return None

    def _later_ready_at(self, current: Any, new: Any) -> Any:
        try:
            return current if current >= new else new
        except Exception:
            return new

    def _defer_row(
        self,
        spec: SourceSpec,
        row_idx: int,
        timed_retry: Optional[TimedRetry],
        *,
        retry_count: int,
    ) -> bool:
        if timed_retry is None:
            return False
        try:
            max_attempts = max(0, int(timed_retry.max_attempts))
        except Exception:
            return False
        if max_attempts <= 0 or retry_count >= max_attempts:
            return False

        key = (spec.name, row_idx)
        existing = self._deferred.get(key)
        ordinal = existing.ordinal if existing else self._deferred_ordinal
        if existing is None:
            self._deferred_ordinal += 1
            ready_at = timed_retry.ready_at
        else:
            ready_at = self._later_ready_at(existing.ready_at, timed_retry.ready_at)

        self._deferred[key] = _DeferredEntry(
            source_name=spec.name,
            row_idx=row_idx,
            retry_count=max(int(existing.retry_count), retry_count) if existing else retry_count,
            ready_at=ready_at,
            max_attempts=max(int(existing.max_attempts), max_attempts) if existing else max_attempts,
            ordinal=ordinal,
        )
        return True

    def _pop_ready_deferred(self, spec: SourceSpec) -> Optional[_DeferredEntry]:
        if not self._deferred or not spec.retry_now:
            return None

        try:
            now = spec.retry_now()
        except Exception:
            return None

        best_key: Optional[Tuple[str, int]] = None
        best_entry: Optional[_DeferredEntry] = None
        stale_keys: List[Tuple[str, int]] = []

        for key, entry in self._deferred.items():
            if entry.source_name != spec.name:
                continue
            if entry.row_idx in self._completed_rows:
                stale_keys.append(key)
                continue
            failed_sources = self._row_source_failures.get(entry.row_idx)
            if failed_sources and spec.name in failed_sources:
                stale_keys.append(key)
                continue
            if entry.retry_count >= entry.max_attempts:
                stale_keys.append(key)
                continue
            try:
                ready = entry.ready_at <= now
            except Exception:
                ready = False
            if not ready:
                continue
            if best_entry is None:
                best_key = key
                best_entry = entry
                continue
            try:
                current_is_earlier = entry.ready_at < best_entry.ready_at
            except Exception:
                current_is_earlier = False
            if current_is_earlier or (
                entry.ready_at == best_entry.ready_at and entry.ordinal < best_entry.ordinal
            ):
                best_key = key
                best_entry = entry

        for key in stale_keys:
            self._deferred.pop(key, None)

        if best_key is None:
            return None
        return self._deferred.pop(best_key, None)

    def _next_work(self, spec: SourceSpec) -> Optional[Tuple[int, int]]:
        deferred = self._pop_ready_deferred(spec)
        if deferred is not None:
            return (deferred.row_idx, deferred.retry_count)
        row_idx = self._next_row(spec)
        if row_idx is None:
            return None
        return (row_idx, 0)

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

    def _adaptive_priority_bonus(self, metrics: MutableMapping[str, object]) -> float:
        attempts = int(metrics.get("attempts", 0))
        enriched = int(metrics.get("enriched", 0))
        if attempts < ADAPTIVE_PRIORITY_MIN_ATTEMPTS:
            return 0.0
        success_rate = enriched / attempts if attempts else 0.0
        return min(ADAPTIVE_PRIORITY_MAX_BONUS, max(0.0, success_rate * ADAPTIVE_PRIORITY_MAX_BONUS))

    def _cooldown_priority_penalty(self, metrics: MutableMapping[str, object]) -> float:
        attempts = int(metrics.get("attempts", 0))
        cooldown = int(metrics.get("cooldown", 0))
        if cooldown <= 0:
            return 0.0
        denom = attempts or cooldown or 1
        cooldown_rate = cooldown / denom if denom else 0.0
        return min(COOLDOWN_PRIORITY_MAX_PENALTY, max(0.0, cooldown_rate * COOLDOWN_PRIORITY_MAX_PENALTY))

    def _compute_priority(self, spec: SourceSpec) -> float:
        metrics = self._metrics.get(spec.name, {})
        attempts = int(metrics.get("attempts", 0))
        cooldown = int(metrics.get("cooldown", 0))

        # Keep early rounds deterministic; only add light jitter once meaningful
        # history exists or cooldown pressure is present.
        jitter = (
            random.uniform(-0.1, 0.1)
            if (attempts >= ADAPTIVE_PRIORITY_MIN_ATTEMPTS or cooldown)
            else 0.0
        )
        adaptive_bonus = self._adaptive_priority_bonus(metrics)
        cooldown_penalty = self._cooldown_priority_penalty(metrics)
        priority = BASE_PRIORITY_SCORE + adaptive_bonus - cooldown_penalty + jitter
        base_score = max(0.2, min(1.5, priority))

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
            adaptive_bonus = self._adaptive_priority_bonus(metrics)
            self._emit(
                f"[Scheduler][Health] {name} success={success_rate:.2f} cooldown={cooldown_rate:.2f} "
                f"attempts={attempts} adaptive_bonus={adaptive_bonus:.2f}"
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
                work_item = self._next_work(spec)
                if work_item is None:
                    continue
                row_idx, retry_count = work_item
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
                        timed_retry = None
                        if spec.unavailable_retry:
                            try:
                                timed_retry = spec.unavailable_retry(row_idx, reason, retry_count)
                            except Exception:
                                timed_retry = None
                        self._defer_row(spec, row_idx, timed_retry, retry_count=retry_count)
                        self._emit(f"[Scheduler] skipping {spec.name} (cooldown) row {display_row}")
                    continue
                self._emit(f"[Scheduler] running {spec.name} for row {display_row}")
                run_row = spec.run_row_retry or (lambda idx, _retry_count: spec.run_row(idx))
                result = run_row(row_idx, retry_count)
                self._record(spec.name, result)
                if result.timed_retry and not result.enriched:
                    self._defer_row(spec, row_idx, result.timed_retry, retry_count=retry_count + 1)
                if result.attempted and not result.enriched and not result.retry_later:
                    self._row_source_failures[row_idx].add(spec.name)
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
