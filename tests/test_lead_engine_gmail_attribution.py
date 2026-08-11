from copy import deepcopy

import pytest

from lead_engine import (
    AttributionClassification,
    GmailDirection,
    OutreachAttributionRegistry,
    build_campaign_export,
    engaged_lead_handoff,
    evaluate_outreach_attribution,
    explicit_mapping_evidence,
    export_timestamp_evidence,
    gmail_message_ref,
    gmail_thread_ref,
    matching_evidence,
    observe_reply,
    outreach_attempt_from_export_row,
    privacy_fingerprint,
    recipient_evidence,
)


EXPORTED_AT = "2026-08-11T14:30:00+10:00"
SENT_AT = "2026-08-12T02:00:00+10:00"


def _export(operation="campaign-one", email="artist@example.com", artist="Artist", source_id="Artist123", **extra):
    row = {
        "Artist": artist,
        "Email": email,
        "Source Directory": "spotify",
        "Source URL": f"https://open.spotify.com/artist/{source_id}",
        **extra,
    }
    return build_campaign_export([row], operation_reference=operation, created_at=EXPORTED_AT)


def _attempt(operation="campaign-one", **kwargs):
    return outreach_attempt_from_export_row(_export(operation=operation, **kwargs).rows[0])


def _outbound(message_id="gmail-message-1", thread_id="gmail-thread-1", recipients=("artist@example.com",), **kwargs):
    return gmail_message_ref(
        gmail_message_id=message_id,
        gmail_thread_id=thread_id,
        direction=GmailDirection.OUTBOUND,
        sender=kwargs.pop("sender", "owner@studiflow.example"),
        recipients=recipients,
        occurred_at=kwargs.pop("occurred_at", SENT_AT),
        **kwargs,
    )


def _inbound(message_id="gmail-reply-1", thread_id="gmail-thread-1"):
    return gmail_message_ref(
        gmail_message_id=message_id,
        gmail_thread_id=thread_id,
        direction=GmailDirection.INBOUND,
        sender="artist@example.com",
        recipients=("owner@studiflow.example",),
        occurred_at="2026-08-12T03:00:00+10:00",
    )


def _exact(attempt, message=None, marker="mapping-record-1"):
    message = message or _outbound()
    return evaluate_outreach_attribution(
        message,
        [attempt],
        {attempt.outreach_attempt_id: [explicit_mapping_evidence(attempt, marker)]},
        evaluated_at="2026-08-12T04:00:00+10:00",
    )


def test_one_export_row_creates_one_distinct_outreach_attempt_identity():
    row = _export().rows[0]
    first = outreach_attempt_from_export_row(row)
    second = outreach_attempt_from_export_row(row)
    assert first == second
    assert first.outreach_attempt_id.startswith("le:outreach-attempt:v1:")
    assert first.export_row_id == row.export_row_id


def test_repeated_exports_to_same_email_create_distinct_attempts():
    january = _attempt(operation="january")
    april = _attempt(operation="april")
    assert january.destination_email == april.destination_email
    assert january.outreach_attempt_id != april.outreach_attempt_id
    assert january.export_row_id != april.export_row_id


def test_email_alone_never_produces_exact_attribution():
    attempt, message = _attempt(), _outbound()
    result = evaluate_outreach_attribution(
        message, [attempt], {attempt.outreach_attempt_id: [recipient_evidence(attempt, message)]}
    )
    assert result.classification == AttributionClassification.HEURISTIC


def test_email_alone_does_not_establish_lead_engine_identity():
    left = _attempt(operation="left", artist="Artist A", source_id="A1")
    right = _attempt(operation="right", artist="Artist B", source_id="B1")
    assert left.destination_email == right.destination_email
    assert left.lead_id != right.lead_id


def test_explicit_persisted_mapping_can_produce_exact():
    assert _exact(_attempt()).classification == AttributionClassification.EXACT


def test_unique_recipient_and_independent_strong_evidence_produce_high_confidence():
    attempt, message = _attempt(), _outbound()
    evidence = [
        recipient_evidence(attempt, message),
        matching_evidence(
            attempt,
            kind="sender_account",
            reason_code="expected_sender_account",
            value=message.sender,
            independent_key="sender_account",
        ),
    ]
    result = evaluate_outreach_attribution(message, [attempt], {attempt.outreach_attempt_id: evidence})
    assert result.classification == AttributionClassification.HIGH_CONFIDENCE


def test_multiple_plausible_export_rows_are_ambiguous():
    first, second, message = _attempt(operation="jan"), _attempt(operation="apr"), _outbound()
    result = evaluate_outreach_attribution(
        message,
        [first, second],
        {
            first.outreach_attempt_id: [recipient_evidence(first, message)],
            second.outreach_attempt_id: [recipient_evidence(second, message)],
        },
    )
    assert result.classification == AttributionClassification.AMBIGUOUS
    assert not result.outreach_attempt_id


def test_conflicting_recipient_evidence_produces_conflict():
    attempt = _attempt()
    message = _outbound(recipients=("someone-else@example.com",))
    result = evaluate_outreach_attribution(
        message, [attempt], {attempt.outreach_attempt_id: [recipient_evidence(attempt, message)]}
    )
    assert result.classification == AttributionClassification.CONFLICT
    assert "recipient_conflict" in result.reason_codes


def test_unmatched_gmail_message_remains_unmatched():
    result = evaluate_outreach_attribution(_outbound(), [_attempt()], {})
    assert result.classification == AttributionClassification.UNMATCHED


def test_pre_existing_gmail_thread_is_not_automatically_attributed_by_participant():
    attempt = _attempt()
    message = _outbound(message_id="old-message", thread_id="old-thread", occurred_at="2025-01-01T00:00:00Z")
    result = evaluate_outreach_attribution(message, [attempt], {})
    assert attempt.destination_email in message.recipients
    assert result.classification == AttributionClassification.UNMATCHED


def test_repeated_campaigns_to_same_email_remain_distinguishable_with_markers():
    january, april = _attempt(operation="january"), _attempt(operation="april")
    january_result = _exact(january, marker="jan-map")
    april_result = _exact(april, message=_outbound(message_id="gmail-april"), marker="apr-map")
    assert january_result.outreach_attempt_id == january.outreach_attempt_id
    assert april_result.outreach_attempt_id == april.outreach_attempt_id


def test_gmail_message_id_remains_provider_owned_identity():
    message = _outbound(message_id="provider-message-token")
    assert message.gmail_message_id == "provider-message-token"
    assert not message.gmail_message_id.startswith("le:")


def test_gmail_thread_id_remains_provider_owned_identity():
    thread = gmail_thread_ref("provider-thread-token", ["ARTIST@example.com"])
    assert thread.gmail_thread_id == "provider-thread-token"
    assert thread.participant_emails == ("artist@example.com",)


def test_gmail_ids_never_replace_lead_id():
    attempt = _attempt()
    result = _exact(attempt)
    assert attempt.lead_id.startswith("le:lead:v1:")
    assert result.gmail_message_id != attempt.lead_id


def test_gmail_ids_never_replace_export_row_id():
    attempt = _attempt()
    result = _exact(attempt)
    assert result.export_row_id == attempt.export_row_id
    assert result.gmail_message_id != result.export_row_id


def test_split_email_export_rows_remain_separate_outreach_attempts():
    base = {
        "Artist": "Split Artist",
        "Source Directory": "spotify",
        "Source URL": "https://open.spotify.com/artist/Split1",
    }
    export = build_campaign_export(
        [{**base, "Email": "one@example.com"}, {**base, "Email": "two@example.com"}],
        operation_reference="split",
        created_at=EXPORTED_AT,
    )
    attempts = tuple(outreach_attempt_from_export_row(row) for row in export.rows)
    assert len({item.outreach_attempt_id for item in attempts}) == 2
    assert {item.destination_email for item in attempts} == {"one@example.com", "two@example.com"}


def test_shared_management_email_does_not_collapse_artist_identity():
    first = _attempt(operation="a", artist="Artist A", email="booking@agency.example", source_id="A")
    second = _attempt(operation="b", artist="Artist B", email="booking@agency.example", source_id="B")
    assert first.destination_email == second.destination_email
    assert first.lead_id != second.lead_id
    assert first.outreach_attempt_id != second.outreach_attempt_id


def test_export_timestamp_alone_does_not_prove_send_attribution():
    attempt, message = _attempt(), _outbound()
    evidence = export_timestamp_evidence(attempt, message, tolerance_seconds=24 * 60 * 60)
    result = evaluate_outreach_attribution(message, [attempt], {attempt.outreach_attempt_id: [evidence]})
    assert evidence.strength == "WEAK"
    assert evidence.tolerance_seconds == 86400
    assert result.classification == AttributionClassification.HEURISTIC


def test_timezone_aware_timestamps_serialize_deterministically():
    message = _outbound(occurred_at="2026-08-12T02:00:00+10:00")
    assert message.occurred_at == "2026-08-11T16:00:00Z"
    with pytest.raises(ValueError, match="timezone"):
        _outbound(occurred_at="2026-08-12T02:00:00")


def test_inbound_reply_can_reference_an_attributed_conversation():
    attribution = _exact(_attempt())
    reply = observe_reply(_inbound(), attribution, observed_at="2026-08-12T03:01:00+10:00")
    assert reply.outreach_attribution_id == attribution.attribution_id
    assert reply.gmail_thread_id == attribution.gmail_thread_id


def test_reply_contract_does_not_imply_positive_response():
    reply = observe_reply(_inbound(), _exact(_attempt()), observed_at="2026-08-12T03:01:00+10:00")
    assert "positive" not in str(reply.to_dict()).casefold()
    assert "interested" not in str(reply.to_dict()).casefold()


def test_reply_contract_does_not_create_crm_lifecycle_state():
    reply = observe_reply(_inbound(), _exact(_attempt()), observed_at="2026-08-12T03:01:00+10:00")
    serialized = str(reply.to_dict()).casefold()
    assert "lifecycle" not in serialized
    assert "stage" not in serialized


def test_handoff_contract_preserves_lead_engine_provenance():
    attempt = _attempt()
    attribution = _exact(attempt)
    reply = observe_reply(_inbound(), attribution, observed_at="2026-08-12T03:01:00+10:00")
    handoff = engaged_lead_handoff(attempt, attribution, reply)
    assert handoff.lead_id == attempt.lead_id
    assert handoff.source_occurrence_id == attempt.source_occurrence_id
    assert handoff.export_row_id == attempt.export_row_id


def test_unresolved_lead_lineage_remains_explicitly_unresolved_in_handoff():
    export = build_campaign_export(
        [{"Artist": "Legacy", "Email": "legacy@example.com", "Source Directory": "legacy"}],
        operation_reference="unresolved",
        created_at=EXPORTED_AT,
    )
    attempt = outreach_attempt_from_export_row(export.rows[0])
    message = _outbound(recipients=("legacy@example.com",))
    attribution = _exact(attempt, message=message)
    reply = observe_reply(_inbound(thread_id=message.gmail_thread_id), attribution, observed_at="2026-08-12T03:01:00+10:00")
    handoff = engaged_lead_handoff(attempt, attribution, reply)
    assert handoff.lineage_status == "UNRESOLVED"
    assert handoff.lead_id == handoff.source_occurrence_id == ""


def test_conflicting_attribution_mappings_are_surfaced_by_registry():
    first, second, message = _attempt(operation="one"), _attempt(operation="two"), _outbound()
    registry = OutreachAttributionRegistry()
    registry.add_attribution(_exact(first, message, "mapping-one"))
    registry.add_attribution(_exact(second, message, "mapping-two"))
    conflicts = registry.conflicting_mappings(message.gmail_message_id)
    assert len(conflicts) == 2


def test_registry_serialization_is_deterministic():
    first, second = _attempt(operation="one"), _attempt(operation="two")
    left, right = OutreachAttributionRegistry(), OutreachAttributionRegistry()
    for registry, ordered in ((left, (first, second)), (right, (second, first))):
        for attempt in ordered:
            registry.add_attempt(attempt)
        registry.add_message(_outbound())
        registry.add_thread(gmail_thread_ref("gmail-thread-1", ["artist@example.com"]))
    assert left.to_json() == right.to_json()


def test_input_fixtures_are_not_mutated():
    export = _export()
    row = export.rows[0]
    message = _outbound()
    attempts = [outreach_attempt_from_export_row(row)]
    evidence = {attempts[0].outreach_attempt_id: [recipient_evidence(attempts[0], message)]}
    before_attempts, before_evidence = deepcopy(attempts), deepcopy(evidence)
    evaluate_outreach_attribution(message, attempts, evidence)
    assert attempts == before_attempts
    assert evidence == before_evidence


def test_contract_has_no_gmail_api_or_network_operation():
    registry = OutreachAttributionRegistry()
    assert not any(name.startswith(("fetch", "query", "send", "connect")) for name in dir(registry))


def test_contract_has_no_woodpecker_operation():
    payload = OutreachAttributionRegistry().to_json().casefold()
    assert "woodpecker_campaign_id" not in payload
    assert "woodpecker_prospect_id" not in payload


def test_contract_has_no_crm_mutation_operation():
    handoff_names = set(engaged_lead_handoff.__annotations__)
    assert "crm_contact_id" not in handoff_names
    assert not any(name.startswith(("create_contact", "update_stage")) for name in dir(OutreachAttributionRegistry))


def test_no_canonical_entity_id_is_generated():
    attempt = _attempt()
    assert "canonical_entity_id" not in attempt.to_json()
    assert "canonical_entity_id" not in _exact(attempt).to_json()


def test_existing_campaign_export_identifiers_remain_unchanged():
    export = _export()
    before_export_id = export.export_id
    before_row_id = export.rows[0].export_row_id
    outreach_attempt_from_export_row(export.rows[0])
    assert export.export_id == before_export_id
    assert export.rows[0].export_row_id == before_row_id


def test_privacy_fingerprint_is_deterministic_and_does_not_retain_content():
    subject = "  Hello   Artist  "
    fingerprint = privacy_fingerprint(subject, field="subject")
    assert fingerprint == privacy_fingerprint("Hello Artist", field="subject")
    assert subject.strip() not in fingerprint


def test_reply_on_different_thread_is_rejected():
    with pytest.raises(ValueError, match="same Gmail thread"):
        observe_reply(
            _inbound(thread_id="different-thread"),
            _exact(_attempt()),
            observed_at="2026-08-12T03:01:00+10:00",
        )
