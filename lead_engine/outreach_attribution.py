"""Pure contracts for attributing Gmail conversations to campaign export rows.

This module performs no Gmail, Woodpecker, CRM, filesystem, or network I/O.
Email addresses are candidate evidence only; they never establish Lead Engine
identity or exact outreach attribution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence, Tuple

from email_normalizer import normalize_email_value

from .export_ledger import CampaignExportRow


OUTREACH_SCHEMA_VERSION = "lead-engine-gmail-outreach-attribution/v1"
OUTREACH_RULE_VERSION = "gmail-outreach-attribution-rules/v1"
OUTREACH_ATTEMPT_ID_PREFIX = "le:outreach-attempt:v1"
ATTRIBUTION_ID_PREFIX = "le:outreach-attribution:v1"
REPLY_OBSERVATION_ID_PREFIX = "le:reply-observation:v1"


class GmailDirection(str, Enum):
    OUTBOUND = "OUTBOUND"
    INBOUND = "INBOUND"


class AttributionClassification(str, Enum):
    EXACT = "EXACT"
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    HEURISTIC = "HEURISTIC"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICT = "CONFLICT"
    UNMATCHED = "UNMATCHED"


@dataclass(frozen=True)
class GmailMessageRef:
    """Provider-owned Gmail message identity and narrow matching metadata."""

    gmail_message_id: str
    gmail_thread_id: str
    direction: GmailDirection
    sender: str
    recipients: Tuple[str, ...]
    occurred_at: str
    rfc_message_id: str = ""
    subject_fingerprint: str = ""
    body_fingerprint: str = ""
    schema_version: str = OUTREACH_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GmailThreadRef:
    """Provider-owned Gmail thread identity; never a Lead Engine identity."""

    gmail_thread_id: str
    participant_emails: Tuple[str, ...] = ()
    schema_version: str = OUTREACH_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OutreachAttempt:
    """One intended outbound contact attempt for exactly one export row."""

    outreach_attempt_id: str
    export_id: str
    export_row_id: str
    lead_id: str
    source_occurrence_id: str
    lineage_status: str
    destination_email: str
    exported_at: str
    row_fingerprint: str
    rule_version: str = OUTREACH_RULE_VERSION
    schema_version: str = OUTREACH_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True)
class AttributionEvidence:
    """One explainable fact; values should be IDs, hashes, or narrow metadata."""

    outreach_attempt_id: str
    kind: str
    relationship: str
    strength: str
    reason_code: str
    value: str = ""
    independent_key: str = ""
    observed_delta_seconds: Optional[int] = None
    tolerance_seconds: Optional[int] = None
    schema_version: str = OUTREACH_SCHEMA_VERSION


@dataclass(frozen=True)
class OutreachAttribution:
    """Explainable association between a Gmail message and zero or one attempt."""

    attribution_id: str
    classification: AttributionClassification
    gmail_message_id: str
    gmail_thread_id: str
    outreach_attempt_id: str
    export_row_id: str
    candidate_outreach_attempt_ids: Tuple[str, ...]
    evidence: Tuple[AttributionEvidence, ...]
    reason_codes: Tuple[str, ...]
    evaluated_at: str = ""
    human_decision: str = ""
    rule_version: str = OUTREACH_RULE_VERSION
    schema_version: str = OUTREACH_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True)
class ReplyObservation:
    """An inbound Gmail reply observed on an attributed conversation."""

    reply_observation_id: str
    gmail_message_id: str
    gmail_thread_id: str
    outreach_attribution_id: str
    observed_at: str
    sender: str
    recipients: Tuple[str, ...]
    attribution_classification: AttributionClassification
    schema_version: str = OUTREACH_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EngagedLeadHandoff:
    """Provider-neutral future CRM input; it contains no CRM lifecycle policy."""

    lead_id: str
    source_occurrence_id: str
    lineage_status: str
    export_id: str
    export_row_id: str
    outreach_attempt_id: str
    gmail_thread_id: str
    outbound_gmail_message_id: str
    reply_gmail_message_id: str
    contacted_email: str
    attribution_classification: AttributionClassification
    attribution_id: str
    reply_observation_id: str
    schema_version: str = OUTREACH_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(prefix: str, payload: object) -> str:
    return f"{prefix}:{hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()}"


def _timestamp(value: str, field_name: str, *, optional: bool = False) -> str:
    text = str(value or "").strip()
    if optional and not text:
        return ""
    if not text:
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalized_emails(values: Sequence[str]) -> Tuple[str, ...]:
    normalized = {normalize_email_value(value) for value in values}
    return tuple(sorted(value for value in normalized if value))


def outreach_attempt_id(export_row_identifier: str) -> str:
    value = str(export_row_identifier or "").strip()
    if not value.startswith("le:campaign-export-row:v1:"):
        raise ValueError("export_row_identifier is not a Campaign Export Row ID")
    return _digest(OUTREACH_ATTEMPT_ID_PREFIX, {"export_row_id": value})


def outreach_attempt_from_export_row(row: CampaignExportRow) -> OutreachAttempt:
    """Adapt a frozen export row without changing or mutating it."""
    return OutreachAttempt(
        outreach_attempt_id=outreach_attempt_id(row.export_row_id),
        export_id=row.export_id,
        export_row_id=row.export_row_id,
        lead_id=row.lead_id,
        source_occurrence_id=row.source_occurrence_id,
        lineage_status=row.lineage.status,
        destination_email=row.contact_destination.normalized_value,
        exported_at=_timestamp(row.exported_at, "exported_at"),
        row_fingerprint=row.row_fingerprint,
    )


def gmail_message_ref(
    *,
    gmail_message_id: str,
    gmail_thread_id: str,
    direction: GmailDirection,
    sender: str,
    recipients: Sequence[str],
    occurred_at: str,
    rfc_message_id: str = "",
    subject_fingerprint: str = "",
    body_fingerprint: str = "",
) -> GmailMessageRef:
    if not str(gmail_message_id).strip() or not str(gmail_thread_id).strip():
        raise ValueError("Gmail message and thread IDs are required provider references")
    return GmailMessageRef(
        gmail_message_id=str(gmail_message_id).strip(),
        gmail_thread_id=str(gmail_thread_id).strip(),
        direction=GmailDirection(direction),
        sender=normalize_email_value(sender),
        recipients=_normalized_emails(recipients),
        occurred_at=_timestamp(occurred_at, "occurred_at"),
        rfc_message_id=str(rfc_message_id or "").strip(),
        subject_fingerprint=str(subject_fingerprint or "").strip(),
        body_fingerprint=str(body_fingerprint or "").strip(),
    )


def gmail_thread_ref(gmail_thread_id: str, participant_emails: Sequence[str] = ()) -> GmailThreadRef:
    if not str(gmail_thread_id).strip():
        raise ValueError("gmail_thread_id is required")
    return GmailThreadRef(str(gmail_thread_id).strip(), _normalized_emails(participant_emails))


def privacy_fingerprint(value: str, *, field: str) -> str:
    """Hash normalized subject/body text so contracts need not retain full content."""
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return ""
    return _digest(f"le:gmail-{field}-fingerprint:v1", {"value": normalized})


def recipient_evidence(attempt: OutreachAttempt, message: GmailMessageRef) -> AttributionEvidence:
    match = attempt.destination_email in message.recipients
    return AttributionEvidence(
        outreach_attempt_id=attempt.outreach_attempt_id,
        kind="recipient",
        relationship="MATCH" if match else "CONFLICT",
        # An exact envelope recipient is a strong routing fact, but the rule
        # still requires another independent strong fact for HIGH_CONFIDENCE.
        strength="STRONG",
        reason_code="recipient_match" if match else "recipient_conflict",
        value=attempt.destination_email,
        independent_key="recipient",
    )


def export_timestamp_evidence(
    attempt: OutreachAttempt,
    message: GmailMessageRef,
    *,
    tolerance_seconds: int,
) -> AttributionEvidence:
    """Compare export/send times transparently; export proximity is always weak."""
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must be non-negative")
    exported = datetime.fromisoformat(attempt.exported_at.replace("Z", "+00:00"))
    occurred = datetime.fromisoformat(message.occurred_at.replace("Z", "+00:00"))
    delta = int((occurred - exported).total_seconds())
    within = 0 <= delta <= tolerance_seconds
    return AttributionEvidence(
        outreach_attempt_id=attempt.outreach_attempt_id,
        kind="export_timestamp_proximity",
        relationship="MATCH" if within else "NONE",
        strength="WEAK",
        reason_code="within_export_time_tolerance" if within else "outside_export_time_tolerance",
        value=attempt.exported_at,
        independent_key="export_timestamp",
        observed_delta_seconds=delta,
        tolerance_seconds=tolerance_seconds,
    )


def explicit_mapping_evidence(attempt: OutreachAttempt, marker: str) -> AttributionEvidence:
    if not str(marker or "").strip():
        raise ValueError("an explicit persisted marker or mapping reference is required")
    return AttributionEvidence(
        outreach_attempt_id=attempt.outreach_attempt_id,
        kind="persisted_mapping",
        relationship="MATCH",
        strength="EXPLICIT",
        reason_code="explicit_persisted_mapping",
        value=str(marker).strip(),
        independent_key="persisted_mapping",
    )


def matching_evidence(
    attempt: OutreachAttempt,
    *,
    kind: str,
    reason_code: str,
    value: str = "",
    strength: str = "STRONG",
    independent_key: str = "",
) -> AttributionEvidence:
    """Build narrow caller-supplied evidence from an audited future integration."""
    if strength not in {"STRONG", "WEAK"}:
        raise ValueError("matching evidence strength must be STRONG or WEAK")
    return AttributionEvidence(
        outreach_attempt_id=attempt.outreach_attempt_id,
        kind=str(kind),
        relationship="MATCH",
        strength=strength,
        reason_code=str(reason_code),
        value=str(value or ""),
        independent_key=str(independent_key or kind),
    )


def evaluate_outreach_attribution(
    message: GmailMessageRef,
    attempts: Sequence[OutreachAttempt],
    evidence_by_attempt: Mapping[str, Sequence[AttributionEvidence]],
    *,
    evaluated_at: str = "",
    human_decision: str = "",
) -> OutreachAttribution:
    """Classify candidates with explicit, inspectable rules and no hidden score."""
    if message.direction != GmailDirection.OUTBOUND:
        raise ValueError("outreach attribution evaluates an outbound Gmail message")
    unique_attempts = {attempt.outreach_attempt_id: attempt for attempt in attempts}
    if len(unique_attempts) != len(attempts):
        raise ValueError("attempt inputs must be unique")

    evidence: list[AttributionEvidence] = []
    plausible: list[str] = []
    exact: list[str] = []
    high: list[str] = []
    conflicted: list[str] = []
    for attempt_id in sorted(unique_attempts):
        items = tuple(evidence_by_attempt.get(attempt_id, ()))
        if any(item.outreach_attempt_id != attempt_id for item in items):
            raise ValueError("evidence references a different outreach attempt")
        evidence.extend(items)
        if any(item.relationship == "CONFLICT" and item.strength in {"STRONG", "EXPLICIT"} for item in items):
            conflicted.append(attempt_id)
            continue
        matches = [item for item in items if item.relationship == "MATCH"]
        if not matches:
            continue
        plausible.append(attempt_id)
        if any(item.strength == "EXPLICIT" for item in matches):
            exact.append(attempt_id)
        strong_keys = {item.independent_key or item.kind for item in matches if item.strength == "STRONG"}
        has_recipient = any(item.reason_code == "recipient_match" for item in matches)
        if has_recipient and len(strong_keys) >= 2:
            high.append(attempt_id)

    selected = ""
    if len(exact) == 1:
        classification, selected = AttributionClassification.EXACT, exact[0]
    elif len(exact) > 1 or len(plausible) > 1:
        classification = AttributionClassification.AMBIGUOUS
    elif len(high) == 1 and len(plausible) == 1:
        classification, selected = AttributionClassification.HIGH_CONFIDENCE, high[0]
    elif len(plausible) == 1:
        classification, selected = AttributionClassification.HEURISTIC, plausible[0]
    elif conflicted:
        classification = AttributionClassification.CONFLICT
    else:
        classification = AttributionClassification.UNMATCHED

    candidates = tuple(sorted(set(plausible + conflicted)))
    selected_attempt = unique_attempts.get(selected)
    sorted_evidence = tuple(sorted(evidence, key=lambda item: (
        item.outreach_attempt_id, item.kind, item.relationship, item.reason_code, item.value
    )))
    reasons = tuple(sorted({item.reason_code for item in sorted_evidence}))
    attribution_identifier = _digest(ATTRIBUTION_ID_PREFIX, {
        "gmail_message_id": message.gmail_message_id,
        "classification": classification.value,
        "selected_attempt_id": selected,
        "candidate_attempt_ids": candidates,
    })
    return OutreachAttribution(
        attribution_id=attribution_identifier,
        classification=classification,
        gmail_message_id=message.gmail_message_id,
        gmail_thread_id=message.gmail_thread_id,
        outreach_attempt_id=selected,
        export_row_id=selected_attempt.export_row_id if selected_attempt else "",
        candidate_outreach_attempt_ids=candidates,
        evidence=sorted_evidence,
        reason_codes=reasons,
        evaluated_at=_timestamp(evaluated_at, "evaluated_at", optional=True),
        human_decision=str(human_decision or ""),
    )


def observe_reply(
    message: GmailMessageRef,
    attribution: OutreachAttribution,
    *,
    observed_at: str,
) -> ReplyObservation:
    if message.direction != GmailDirection.INBOUND:
        raise ValueError("a reply observation requires an inbound Gmail message")
    if not attribution.outreach_attempt_id:
        raise ValueError("a reply requires a resolved outreach attribution")
    if message.gmail_thread_id != attribution.gmail_thread_id:
        raise ValueError("reply and outreach attribution must reference the same Gmail thread")
    observed = _timestamp(observed_at, "observed_at")
    identifier = _digest(REPLY_OBSERVATION_ID_PREFIX, {
        "gmail_message_id": message.gmail_message_id,
        "outreach_attribution_id": attribution.attribution_id,
    })
    return ReplyObservation(
        reply_observation_id=identifier,
        gmail_message_id=message.gmail_message_id,
        gmail_thread_id=message.gmail_thread_id,
        outreach_attribution_id=attribution.attribution_id,
        observed_at=observed,
        sender=message.sender,
        recipients=message.recipients,
        attribution_classification=attribution.classification,
    )


def engaged_lead_handoff(
    attempt: OutreachAttempt,
    attribution: OutreachAttribution,
    reply: ReplyObservation,
) -> EngagedLeadHandoff:
    if attribution.outreach_attempt_id != attempt.outreach_attempt_id:
        raise ValueError("attribution does not reference the supplied outreach attempt")
    if reply.outreach_attribution_id != attribution.attribution_id:
        raise ValueError("reply does not reference the supplied attribution")
    return EngagedLeadHandoff(
        lead_id=attempt.lead_id,
        source_occurrence_id=attempt.source_occurrence_id,
        lineage_status=attempt.lineage_status,
        export_id=attempt.export_id,
        export_row_id=attempt.export_row_id,
        outreach_attempt_id=attempt.outreach_attempt_id,
        gmail_thread_id=attribution.gmail_thread_id,
        outbound_gmail_message_id=attribution.gmail_message_id,
        reply_gmail_message_id=reply.gmail_message_id,
        contacted_email=attempt.destination_email,
        attribution_classification=attribution.classification,
        attribution_id=attribution.attribution_id,
        reply_observation_id=reply.reply_observation_id,
    )


class OutreachAttributionRegistry:
    """Deterministic in-memory index; deliberately not production persistence."""

    def __init__(self) -> None:
        self._attempts: Dict[str, OutreachAttempt] = {}
        self._messages: Dict[str, GmailMessageRef] = {}
        self._threads: Dict[str, GmailThreadRef] = {}
        self._attributions: Dict[str, OutreachAttribution] = {}
        self._replies: Dict[str, ReplyObservation] = {}

    def add_attempt(self, attempt: OutreachAttempt) -> OutreachAttempt:
        self._add_immutable(self._attempts, attempt.outreach_attempt_id, attempt)
        return attempt

    def add_message(self, message: GmailMessageRef) -> GmailMessageRef:
        self._add_immutable(self._messages, message.gmail_message_id, message)
        return message

    def add_thread(self, thread: GmailThreadRef) -> GmailThreadRef:
        self._add_immutable(self._threads, thread.gmail_thread_id, thread)
        return thread

    def add_attribution(self, attribution: OutreachAttribution) -> OutreachAttribution:
        self._add_immutable(self._attributions, attribution.attribution_id, attribution)
        return attribution

    def add_reply(self, reply: ReplyObservation) -> ReplyObservation:
        self._add_immutable(self._replies, reply.reply_observation_id, reply)
        return reply

    @staticmethod
    def _add_immutable(store: dict, identifier: str, value: object) -> None:
        existing = store.get(identifier)
        if existing is not None and existing != value:
            raise ValueError(f"{identifier} already exists with different content")
        store[identifier] = value

    def get_attempt(self, attempt_id: str) -> Optional[OutreachAttempt]:
        return self._attempts.get(attempt_id)

    def get_message(self, gmail_message_id: str) -> Optional[GmailMessageRef]:
        return self._messages.get(gmail_message_id)

    def get_thread(self, gmail_thread_id: str) -> Optional[GmailThreadRef]:
        return self._threads.get(gmail_thread_id)

    def attempts_by_export_row(self, export_row_id: str) -> Tuple[OutreachAttempt, ...]:
        return self._find(self._attempts, lambda item: item.export_row_id == export_row_id)

    def attributions_by_message(self, gmail_message_id: str) -> Tuple[OutreachAttribution, ...]:
        return self._find(self._attributions, lambda item: item.gmail_message_id == gmail_message_id)

    def attributions_by_thread(self, gmail_thread_id: str) -> Tuple[OutreachAttribution, ...]:
        return self._find(self._attributions, lambda item: item.gmail_thread_id == gmail_thread_id)

    def conflicting_mappings(self, gmail_message_id: str) -> Tuple[OutreachAttribution, ...]:
        items = self.attributions_by_message(gmail_message_id)
        selected = {item.outreach_attempt_id for item in items if item.outreach_attempt_id}
        if len(selected) > 1 or any(item.classification == AttributionClassification.CONFLICT for item in items):
            return items
        return ()

    @staticmethod
    def _find(store: Mapping[str, object], predicate) -> tuple:
        return tuple(store[key] for key in sorted(store) if predicate(store[key]))

    def to_dict(self) -> dict:
        return {
            "schema_version": OUTREACH_SCHEMA_VERSION,
            "attempts": [asdict(self._attempts[key]) for key in sorted(self._attempts)],
            "messages": [asdict(self._messages[key]) for key in sorted(self._messages)],
            "threads": [asdict(self._threads[key]) for key in sorted(self._threads)],
            "attributions": [asdict(self._attributions[key]) for key in sorted(self._attributions)],
            "reply_observations": [asdict(self._replies[key]) for key in sorted(self._replies)],
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())
