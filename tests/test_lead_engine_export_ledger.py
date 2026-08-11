from copy import deepcopy

import pytest

from lead_engine import (
    CampaignExportLedger,
    build_campaign_export,
    compare_identity_rows,
    export_id,
    export_row_id,
    lead_record_from_row,
    source_occurrence_from_row,
)


CREATED_AT = "2026-08-11T14:30:00+10:00"


def _legacy_spotify_row(artist="Ledger Artist", email="Artist@Example.COM", spotify_id="Ledger123", **extra):
    return {
        "Artist": artist,
        "Location": "Melbourne",
        "Email": email,
        "Source Directory": "spotify",
        "Source URL": f"https://open.spotify.com/artist/{spotify_id}",
        **extra,
    }


def _explicit_lineage_row(**extra):
    source = {
        "Artist Name": "Explicit Artist",
        "Source Directory": "spotify",
        "Spotify Artist ID": "Explicit123",
    }
    occurrence = source_occurrence_from_row(source)
    lead = lead_record_from_row(source)
    return {
        "Artist": "Explicit Artist",
        "Email": "explicit@example.com",
        "lead_id": lead.lead_id,
        "source_occurrence_id": occurrence.source_occurrence_id,
        **extra,
    }


def _build(rows, operation="export-operation-001", **kwargs):
    return build_campaign_export(
        rows,
        operation_reference=operation,
        created_at=CREATED_AT,
        **kwargs,
    )


def test_export_operation_identity_is_stable_and_caller_scoped():
    assert export_id(" export-operation-001 ") == export_id("export-operation-001")
    assert export_id("export-operation-001") != export_id("export-operation-002")
    assert export_id("export-operation-001").startswith("le:campaign-export:v1:")
    with pytest.raises(ValueError, match="operation_reference"):
        export_id("  ")


def test_identical_content_can_be_two_operations_with_matching_content_fingerprint():
    first = _build([_legacy_spotify_row()], operation="export-one")
    second = _build([_legacy_spotify_row()], operation="export-two")
    assert first.export_id != second.export_id
    assert first.content_fingerprint == second.content_fingerprint
    assert first.rows[0].row_fingerprint == second.rows[0].row_fingerprint
    assert first.rows[0].export_row_id != second.rows[0].export_row_id


def test_export_row_id_is_position_within_operation_not_artist_identity():
    operation = export_id("operation-row-identity")
    assert export_row_id(operation, 1) == export_row_id(operation, 1)
    assert export_row_id(operation, 1) != export_row_id(operation, 2)
    with pytest.raises(ValueError, match="one-based"):
        export_row_id(operation, 0)


def test_row_position_and_lineage_are_preserved():
    export = _build([_explicit_lineage_row(), _legacy_spotify_row()], operation="positions")
    assert [row.row_position for row in export.rows] == [1, 2]
    expected = _explicit_lineage_row()
    assert export.rows[0].lead_id == expected["lead_id"]
    assert export.rows[0].source_occurrence_id == expected["source_occurrence_id"]
    assert export.rows[0].lineage.status == "RESOLVED"
    assert export.rows[0].lineage.resolution_method == "explicit_validated"


def test_strong_legacy_provider_evidence_derives_lineage_safely():
    export = _build([_legacy_spotify_row()], operation="derived-lineage")
    lineage = export.rows[0].lineage
    assert lineage.status == "RESOLVED"
    assert lineage.resolution_method == "derived_from_strong_legacy_source_evidence"
    assert lineage.lead_id.startswith("le:lead:v1:")
    assert lineage.source_occurrence_id.startswith("le:source-occurrence:v1:spotify:")


def test_weak_legacy_name_fallback_remains_explicitly_unresolved():
    export = _build(
        [{"Artist": "Legacy Artist", "Email": "legacy@example.com", "Source Directory": "old_directory"}],
        operation="weak-lineage",
    )
    lineage = export.rows[0].lineage
    assert lineage.status == "UNRESOLVED"
    assert lineage.lead_id == ""
    assert lineage.source_occurrence_id == ""
    assert "not source-specific strong evidence" in lineage.reason


def test_invalid_or_conflicting_explicit_lineage_fails_closed():
    row = _explicit_lineage_row()
    row["lead_id"] = "le:lead:v1:" + "0" * 64
    lineage = _build([row], operation="conflicting-lineage").rows[0].lineage
    assert lineage.status == "UNRESOLVED"
    assert lineage.lead_id == ""
    assert lineage.resolution_method == "explicit_conflict"


def test_identity_assertion_id_is_not_accepted_as_lead_identity():
    assertion = compare_identity_rows(_legacy_spotify_row(), _legacy_spotify_row())
    row = {"Artist": "Wrong Reference", "Email": "wrong@example.com", "lead_id": assertion.assertion_id}
    lineage = _build([row], operation="wrong-reference").rows[0].lineage
    assert lineage.status == "UNRESOLVED"
    assert lineage.lead_id == ""
    assert lineage.raw_lead_reference == assertion.assertion_id


def test_exported_contact_preserves_raw_normalized_type_and_provenance():
    row = _legacy_spotify_row(
        email="  Artist@Example.COM  ",
        Contact_Type="management",
        Email_Source_Type="website_enrich",
        Email_Source_URL="https://artist.example/contact",
        Email_Extract_Method="regex",
    )
    contact = _build([row], operation="contact").rows[0].contact_destination
    assert contact.raw_value == "  Artist@Example.COM  "
    assert contact.normalized_value == "artist@example.com"
    assert contact.contact_type == "management"
    assert contact.provenance_source_type == "website_enrich"
    assert contact.provenance_source_url == "https://artist.example/contact"
    assert contact.extraction_method == "regex"


def test_aliases_and_irrelevant_csv_formatting_produce_same_row_fingerprint():
    output_shaped = {
        "Email": " Artist@Example.COM ",
        "First Name": "Alex",
        "Company": "Ledger Artist",
        "Artist": "Ledger Artist",
        "Location": "Melbourne   VIC",
        "Song Title": "New Song",
        "Website": "HTTPS://EXAMPLE.COM/",
        "Source URL": "https://open.spotify.com/artist/Ledger123/",
        "Source_Directory": "spotify",
        "Source Directory": "Spotify",
        "Notes": "Personal  note",
    }
    input_shaped = {
        "Primary_Email": "artist@example.com",
        "Contact_Name": "Alex",
        "Organization": "Ledger Artist",
        "Artist": "Ledger Artist",
        "location": "Melbourne VIC",
        "Song_Title": "New  Song",
        "Website": "https://example.com",
        "Source_URL": "https://open.spotify.com/artist/Ledger123",
        "Source_Directory": "spotify",
        "Notes": "Personal note",
    }
    first = _build([output_shaped], operation="format-one").rows[0]
    second = _build([input_shaped], operation="format-two").rows[0]
    assert first.row_fingerprint == second.row_fingerprint


def test_material_destination_or_personalization_change_changes_row_fingerprint():
    base = _legacy_spotify_row(Notes="Hello artist")
    changed_email = _legacy_spotify_row(email="other@example.com", Notes="Hello artist")
    changed_note = _legacy_spotify_row(Notes="Different message context")
    fingerprints = {
        _build([base], operation="material-one").rows[0].row_fingerprint,
        _build([changed_email], operation="material-two").rows[0].row_fingerprint,
        _build([changed_note], operation="material-three").rows[0].row_fingerprint,
    }
    assert len(fingerprints) == 3


def test_profile_version_participates_in_row_and_content_fingerprint():
    first = _build([_legacy_spotify_row()], operation="profile-one")
    second = _build(
        [_legacy_spotify_row()],
        operation="profile-two",
        export_profile_version="campaign-prep-woodpecker/v2",
    )
    assert first.rows[0].row_fingerprint != second.rows[0].row_fingerprint
    assert first.content_fingerprint != second.content_fingerprint


def test_batch_content_fingerprint_is_order_insensitive_but_duplicate_sensitive():
    a = _legacy_spotify_row(artist="Artist A", email="a@example.com", spotify_id="A123")
    b = _legacy_spotify_row(artist="Artist B", email="b@example.com", spotify_id="B123")
    first = _build([a, b], operation="order-one")
    reordered = _build([b, a], operation="order-two")
    duplicated = _build([a, b, b], operation="order-three")
    assert first.content_fingerprint == reordered.content_fingerprint
    assert first.content_fingerprint != duplicated.content_fingerprint


def test_multiple_rows_can_reference_same_lead_for_split_destinations():
    base = _explicit_lineage_row()
    export = _build(
        [{**base, "Email": "first@example.com"}, {**base, "Email": "second@example.com"}],
        operation="split-emails",
    )
    assert export.row_count == 2
    assert export.rows[0].lead_id == export.rows[1].lead_id
    assert export.rows[0].export_row_id != export.rows[1].export_row_id
    assert export.rows[0].row_fingerprint != export.rows[1].row_fingerprint


def test_one_export_contains_many_leads_without_collapsing_shared_email():
    rows = [
        _legacy_spotify_row(artist="Artist A", email="booking@agency.example", spotify_id="A123"),
        _legacy_spotify_row(artist="Artist B", email="booking@agency.example", spotify_id="B123"),
    ]
    export = _build(rows, operation="shared-email")
    assert export.row_count == 2
    assert export.rows[0].contact_destination.normalized_value == export.rows[1].contact_destination.normalized_value
    assert export.rows[0].row_fingerprint != export.rows[1].row_fingerprint
    assert export.rows[0].lead_id != export.rows[1].lead_id


def test_duplicate_outbound_content_is_detected_without_deduplication():
    row = _legacy_spotify_row()
    export = _build([row, dict(row)], operation="duplicate-content")
    ledger = CampaignExportLedger()
    ledger.add_export(export)
    assert export.row_count == 2
    assert export.rows[0].row_fingerprint == export.rows[1].row_fingerprint
    assert export.rows[0].export_row_id != export.rows[1].export_row_id
    assert ledger.find_rows_by_fingerprint(export.rows[0].row_fingerprint) == export.rows


def test_ledger_indexes_export_row_lead_email_and_content():
    export = _build([_explicit_lineage_row()], operation="indexes")
    ledger = CampaignExportLedger()
    ledger.add_export(export)
    row = export.rows[0]
    assert ledger.get_export(export.export_id) == export
    assert ledger.get_row(row.export_row_id) == row
    assert ledger.find_rows_by_lead_id(row.lead_id) == (row,)
    assert ledger.find_rows_by_email(" EXPLICIT@EXAMPLE.COM ") == (row,)
    assert ledger.find_exports_by_content_fingerprint(export.content_fingerprint) == (export,)


def test_ledger_and_row_serialization_are_deterministic():
    first = _build([_legacy_spotify_row()], operation="serialize-one")
    second = _build([_legacy_spotify_row(artist="Other", spotify_id="Other1")], operation="serialize-two")
    ledger_a = CampaignExportLedger()
    ledger_b = CampaignExportLedger()
    for export in (first, second):
        ledger_a.add_export(export)
    for export in (second, first):
        ledger_b.add_export(export)
    assert first.rows[0].to_json() == first.rows[0].to_json()
    assert ledger_a.to_json() == ledger_b.to_json()


def test_same_operation_reference_is_idempotent_but_conflicting_reuse_fails():
    ledger = CampaignExportLedger()
    first = _build([_legacy_spotify_row()], operation="idempotent")
    ledger.add_export(first)
    assert ledger.add_export(first) == first
    changed = _build([_legacy_spotify_row(email="changed@example.com")], operation="idempotent")
    with pytest.raises(ValueError, match="different operation content"):
        ledger.add_export(changed)


def test_input_rows_are_not_mutated():
    row = _legacy_spotify_row(Notes="  preserve raw  ")
    before = deepcopy(row)
    _build([row], operation="immutability")
    assert row == before


def test_timestamp_is_explicit_and_normalized_without_clock_access():
    export = _build([_legacy_spotify_row()], operation="timestamp")
    assert export.created_at == "2026-08-11T04:30:00Z"
    assert export.rows[0].exported_at == export.created_at
    with pytest.raises(ValueError, match="timezone"):
        build_campaign_export([], operation_reference="no-timezone", created_at="2026-08-11T04:30:00")


def test_contract_generates_no_canonical_woodpecker_or_analytics_identifiers():
    payload = _build([_legacy_spotify_row()], operation="scope-boundary").to_dict()
    serialized = str(payload).casefold()
    assert "canonical_entity_id" not in serialized
    assert "woodpecker_campaign_id" not in serialized
    assert "woodpecker_prospect_id" not in serialized
    assert "analytics_event" not in serialized


def test_existing_foundation_identifiers_remain_unchanged():
    source = {
        "Artist Name": "Stable Artist",
        "Source Directory": "spotify",
        "Spotify Artist ID": "Stable123",
    }
    assert source_occurrence_from_row(source).source_occurrence_id == (
        "le:source-occurrence:v1:spotify:ba1d42c900f28e39225e29a2244544ac67a242082b06133a464e066bf925299f"
    )
    assert lead_record_from_row(source).lead_id == (
        "le:lead:v1:b3bf8cf64687cc02c4619571f55ee915df0bae55a83b57d0466c881a006a1250"
    )
