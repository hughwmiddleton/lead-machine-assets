"""Deterministic pairwise identity assertions and an in-memory registry."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple

from .identity_evidence import IdentityProfile, identity_profile_from_row

IDENTITY_ASSERTION_SCHEMA_VERSION = "lead-engine-identity-assertion/v1"
IDENTITY_ASSERTION_RULE_VERSION = "identity-pair-rules/v1"
ASSERTION_ID_PREFIX = "le:identity-assertion:v1"


class IdentityClassification(str, Enum):
    EXACT = "EXACT"
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    HEURISTIC = "HEURISTIC"
    CONFLICT = "CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"


class HumanDecisionType(str, Enum):
    CONFIRMED_SAME = "CONFIRMED_SAME"
    CONFIRMED_DIFFERENT = "CONFIRMED_DIFFERENT"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class ComparisonEvidence:
    family: str
    kind: str
    relationship: str
    reason_code: str
    left_value: str
    right_value: str
    strength: str
    independence_key: str
    schema_version: str = IDENTITY_ASSERTION_SCHEMA_VERSION


@dataclass(frozen=True)
class HumanIdentityDecision:
    assertion_id: str
    decision: HumanDecisionType
    reason: str = ""
    actor_id: str = ""
    decided_at: str = ""
    schema_version: str = IDENTITY_ASSERTION_SCHEMA_VERSION


@dataclass(frozen=True)
class IdentityAssertion:
    """An advisory relationship between an unordered pair of occurrences."""

    assertion_id: str
    left_source_occurrence_id: str
    right_source_occurrence_id: str
    classification: IdentityClassification
    evidence: Tuple[ComparisonEvidence, ...]
    reason_codes: Tuple[str, ...]
    conflict_indicators: Tuple[str, ...]
    auto_join_eligible: bool
    effective_outcome: str
    human_decision: Optional[HumanIdentityDecision] = None
    rule_version: str = IDENTITY_ASSERTION_RULE_VERSION
    schema_version: str = IDENTITY_ASSERTION_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def assertion_id(left_source_occurrence_id: str, right_source_occurrence_id: str) -> str:
    pair = sorted((str(left_source_occurrence_id), str(right_source_occurrence_id)))
    if not pair[0] or not pair[1]:
        raise ValueError("assertion identity requires two source occurrence IDs")
    payload = {"left": pair[0], "right": pair[1]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{ASSERTION_ID_PREFIX}:{hashlib.sha256(encoded).hexdigest()}"


def _signal_map(profile: IdentityProfile) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for signal in profile.signals:
        result.setdefault(signal.kind, set()).add(signal.value)
    return result


_PROVIDER_REASONS = {
    "spotify_artist_id": "same_spotify_artist_id",
    "bandcamp_artist_host": "same_bandcamp_profile",
    "bandcamp_profile_url": "same_bandcamp_profile",
    "soundcloud_handle": "same_soundcloud_handle",
    "unearthed_artist_slug": "same_unearthed_profile",
    "lastfm_artist_path": "same_lastfm_profile",
    "source_native_id": "same_source_native_id",
}

_MATCH_REASONS = {
    "instagram_handle": "same_instagram_handle",
    "facebook_profile": "same_facebook_profile",
    "artist_domain": "same_artist_domain",
    "website_domain": "same_website_domain",
    "direct_email": "same_direct_email",
    "shared_or_unclassified_email": "same_shared_email",
    "artist_name": "same_artist_name",
    "location": "same_location",
}


def _comparison(kind: str, relationship: str, left: str, right: str, strength: str, reason: str) -> ComparisonEvidence:
    family = "provider" if kind in _PROVIDER_REASONS or kind.endswith("_source_native_id") else {
        "instagram_handle": "social",
        "facebook_profile": "social",
        "artist_domain": "website",
        "website_domain": "website",
        "direct_email": "contact",
        "shared_or_unclassified_email": "contact",
        "artist_name": "context",
        "location": "context",
    }[kind]
    return ComparisonEvidence(
        family=family,
        kind=kind,
        relationship=relationship,
        reason_code=reason,
        left_value=left,
        right_value=right,
        strength=strength,
        independence_key=f"{family}:{kind}",
    )


def compare_identity_profiles(
    left: IdentityProfile,
    right: IdentityProfile,
    *,
    human_decision: Optional[HumanIdentityDecision] = None,
) -> IdentityAssertion:
    """Evaluate one unordered pair. This function never merges or clusters."""
    left_order_key = (left.source_occurrence.source_occurrence_id, left.to_json())
    right_order_key = (right.source_occurrence.source_occurrence_id, right.to_json())
    if left_order_key > right_order_key:
        left, right = right, left
    left_id = left.source_occurrence.source_occurrence_id
    right_id = right.source_occurrence.source_occurrence_id
    pair_assertion_id = assertion_id(left_id, right_id)
    if human_decision is not None and human_decision.assertion_id != pair_assertion_id:
        raise ValueError("human decision assertion_id does not match occurrence pair")

    left_values = _signal_map(left)
    right_values = _signal_map(right)
    evidence: list[ComparisonEvidence] = []
    provider_match = False
    strong_conflict = False

    provider_kinds = {
        signal.kind
        for profile in (left, right)
        for signal in profile.signals
        if signal.family == "provider"
    }
    for kind in sorted(provider_kinds):
        reason = _PROVIDER_REASONS.get(kind, "same_source_native_id")
        left_set = left_values.get(kind, set())
        right_set = right_values.get(kind, set())
        if not left_set or not right_set:
            continue
        shared = sorted(left_set & right_set)
        if shared:
            provider_match = True
            for value in shared:
                evidence.append(_comparison(kind, "match", value, value, "strong", reason))
        if not shared and left_set.isdisjoint(right_set):
            strong_conflict = True
            evidence.append(
                _comparison(
                    kind,
                    "conflict",
                    sorted(left_set)[0],
                    sorted(right_set)[0],
                    "strong",
                    f"different_{kind}",
                )
            )

    for kind, reason in _MATCH_REASONS.items():
        left_set = left_values.get(kind, set())
        right_set = right_values.get(kind, set())
        if not left_set or not right_set:
            continue
        for value in sorted(left_set & right_set):
            strength = {
                "artist_domain": "strong",
                "instagram_handle": "strong",
                "facebook_profile": "strong",
                "direct_email": "strong",
                "website_domain": "corroborative",
                "shared_or_unclassified_email": "weak",
            }.get(kind, "contextual")
            evidence.append(_comparison(kind, "match", value, value, strength, reason))

    same_name = bool(left_values.get("artist_name", set()) & right_values.get("artist_name", set()))
    if same_name:
        for kind, reason in (("location", "location_conflict"), ("artist_domain", "different_artist_domain")):
            left_set = left_values.get(kind, set())
            right_set = right_values.get(kind, set())
            if left_set and right_set and left_set.isdisjoint(right_set):
                strength = "strong" if kind == "artist_domain" else "contextual"
                evidence.append(_comparison(kind, "conflict", sorted(left_set)[0], sorted(right_set)[0], strength, reason))
                if kind == "artist_domain":
                    strong_conflict = True

    evidence = sorted(
        {(
            item.family,
            item.kind,
            item.relationship,
            item.reason_code,
            item.left_value,
            item.right_value,
        ): item for item in evidence}.values(),
        key=lambda item: (item.family, item.kind, item.relationship, item.reason_code, item.left_value, item.right_value),
    )
    matching_strong = [item for item in evidence if item.relationship == "match" and item.strength == "strong"]
    corroborating_families = {
        item.family
        for item in matching_strong
        if item.family in {"social", "website", "contact"}
    }

    if strong_conflict:
        classification = IdentityClassification.CONFLICT
    elif provider_match:
        classification = IdentityClassification.EXACT
    elif same_name and len(corroborating_families) >= 2:
        classification = IdentityClassification.HIGH_CONFIDENCE
    elif same_name:
        classification = IdentityClassification.HEURISTIC
    else:
        classification = IdentityClassification.INSUFFICIENT

    automated_auto_join = classification in {IdentityClassification.EXACT, IdentityClassification.HIGH_CONFIDENCE}
    auto_join = automated_auto_join
    effective_outcome = (
        "ADVISORY_SAME"
        if automated_auto_join
        else "ADVISORY_DIFFERENT"
        if classification == IdentityClassification.CONFLICT
        else "UNRESOLVED"
    )
    if human_decision is not None:
        if human_decision.decision == HumanDecisionType.CONFIRMED_DIFFERENT:
            auto_join = False
            effective_outcome = "CONFIRMED_DIFFERENT"
        elif human_decision.decision == HumanDecisionType.CONFIRMED_SAME:
            auto_join = True
            effective_outcome = "CONFIRMED_SAME"
        else:
            auto_join = automated_auto_join

    reason_codes = tuple(sorted({item.reason_code for item in evidence}))
    conflicts = tuple(sorted({item.reason_code for item in evidence if item.relationship == "conflict"}))
    return IdentityAssertion(
        assertion_id=pair_assertion_id,
        left_source_occurrence_id=left_id,
        right_source_occurrence_id=right_id,
        classification=classification,
        evidence=tuple(evidence),
        reason_codes=reason_codes,
        conflict_indicators=conflicts,
        auto_join_eligible=auto_join,
        effective_outcome=effective_outcome,
        human_decision=human_decision,
    )


def compare_identity_rows(
    left_row: Mapping[object, object],
    right_row: Mapping[object, object],
    *,
    human_decision: Optional[HumanIdentityDecision] = None,
) -> IdentityAssertion:
    return compare_identity_profiles(
        identity_profile_from_row(left_row),
        identity_profile_from_row(right_row),
        human_decision=human_decision,
    )


class IdentityEvidenceRegistry:
    """In-memory pairwise assertion registry; deliberately has no clustering API."""

    def __init__(self) -> None:
        self._assertions: Dict[str, IdentityAssertion] = {}

    def record(self, assertion: IdentityAssertion) -> IdentityAssertion:
        self._assertions[assertion.assertion_id] = assertion
        return assertion

    def compare_and_record(
        self,
        left: IdentityProfile,
        right: IdentityProfile,
    ) -> IdentityAssertion:
        return self.record(compare_identity_profiles(left, right))

    def set_human_decision(self, decision: HumanIdentityDecision) -> IdentityAssertion:
        assertion = self._assertions.get(decision.assertion_id)
        if assertion is None:
            raise KeyError(f"unknown assertion_id: {decision.assertion_id}")
        updated = compare_identity_profiles_from_assertion(assertion, decision)
        return self.record(updated)

    def get(self, assertion_identifier: str) -> Optional[IdentityAssertion]:
        return self._assertions.get(assertion_identifier)

    def assertions(self) -> Tuple[IdentityAssertion, ...]:
        return tuple(self._assertions[key] for key in sorted(self._assertions))

    def to_dict(self) -> dict:
        return {
            "schema_version": IDENTITY_ASSERTION_SCHEMA_VERSION,
            "assertions": [assertion.to_dict() for assertion in self.assertions()],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compare_identity_profiles_from_assertion(
    assertion: IdentityAssertion,
    decision: HumanIdentityDecision,
) -> IdentityAssertion:
    """Apply/clear a human override without altering automated evidence."""
    if decision.assertion_id != assertion.assertion_id:
        raise ValueError("human decision assertion_id does not match assertion")
    automated_auto_join = assertion.classification in {
        IdentityClassification.EXACT,
        IdentityClassification.HIGH_CONFIDENCE,
    }
    if decision.decision == HumanDecisionType.CONFIRMED_DIFFERENT:
        auto_join, outcome = False, "CONFIRMED_DIFFERENT"
    elif decision.decision == HumanDecisionType.CONFIRMED_SAME:
        auto_join, outcome = True, "CONFIRMED_SAME"
    else:
        auto_join = automated_auto_join
        outcome = (
            "ADVISORY_SAME"
            if automated_auto_join
            else "ADVISORY_DIFFERENT"
            if assertion.classification == IdentityClassification.CONFLICT
            else "UNRESOLVED"
        )
    return replace(assertion, auto_join_eligible=auto_join, effective_outcome=outcome, human_decision=decision)
