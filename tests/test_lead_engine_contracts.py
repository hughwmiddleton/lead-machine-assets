import json

from lead_engine import Evidence, LeadRecord, lead_record_from_row, source_occurrence_from_row
from lead_engine.contracts import CONTRACT_SCHEMA_VERSION


def test_lead_id_is_deterministic_and_namespaced_from_source_occurrence():
    row = {
        "Artist Name": "Deterministic Artist",
        "Source Directory": "spotify",
        "Spotify Artist ID": "Deterministic123",
    }
    first = lead_record_from_row(row)
    second = lead_record_from_row(dict(reversed(list(row.items()))))
    assert first.lead_id == second.lead_id
    assert first.lead_id.startswith("le:lead:v1:")
    assert first.source_occurrence_ids == (source_occurrence_from_row(row).source_occurrence_id,)


def test_multiple_evidence_items_reference_one_occurrence_and_preserve_source():
    row = {
        "Artist Name": "  Mixed Case Artist  ",
        "Location": "  Naarm / Melbourne ",
        "Country": "Australia",
        "Email": "Artist@Example.COM",
        "Source Directory": "Bandcamp",
        "Source URL": "https://mixedcaseartist.bandcamp.com/album/debut",
    }
    occurrence = source_occurrence_from_row(row)
    record = lead_record_from_row(row)
    assert len(record.provenance) == 4
    assert {item.source_occurrence_id for item in record.provenance} == {occurrence.source_occurrence_id}
    assert {item.source_type for item in record.provenance} == {"bandcamp"}
    assert {item.source_url for item in record.provenance} == {"https://mixedcaseartist.bandcamp.com/album/debut"}
    assert next(item.value for item in record.provenance if item.fact_name == "artist_name") == "  Mixed Case Artist  "
    assert next(item.value for item in record.provenance if item.fact_name == "email") == "Artist@Example.COM"
    assert record.artist_name_normalized == "mixed case artist"


def test_raw_source_url_coexists_with_normalized_identity_value():
    row = {
        "Artist Name": "Raw URL Artist",
        "Source Directory": "local_seed",
        "Source URL": "  HTTPS://EXAMPLE.COM/artists/raw-url-artist/  ",
    }
    occurrence = source_occurrence_from_row(row)
    record = lead_record_from_row(row)
    assert occurrence.source_url == row["Source URL"]
    assert occurrence.identity_value == "https://example.com/artists/raw-url-artist"
    assert {item.source_url for item in record.provenance} == {row["Source URL"]}


def test_one_lead_record_can_carry_multiple_provenance_items():
    source_id = "le:source-occurrence:v1:test:" + "a" * 64
    evidence = (
        Evidence("artist_name", "Raw Artist", source_id, source_type="test"),
        Evidence("location", "Raw Place", source_id, source_type="test"),
    )
    record = LeadRecord("le:lead:v1:" + "b" * 64, (source_id,), provenance=evidence)
    assert record.provenance == evidence


def test_missing_provenance_values_do_not_fabricate_attribution():
    record = lead_record_from_row(
        {
            "Source Directory": "spotify",
            "Source URL": "https://open.spotify.com/user/no-fact-fields",
            "final_status": "PASS",
        }
    )
    assert record.provenance == ()


def test_contract_serialization_is_deterministic_and_schema_versioned():
    row = {
        "Artist Name": "Serialize Me",
        "Source Directory": "lastfm",
        "Source URL": "https://www.last.fm/music/Serialize+Me",
        "Location": "Melbourne",
    }
    record = lead_record_from_row(row)
    serialized = record.to_json()
    assert serialized == record.to_json()
    payload = json.loads(serialized)
    assert payload["schema_version"] == CONTRACT_SCHEMA_VERSION
    assert payload["provenance"][0]["value"] == "Serialize Me"
    assert "canonical_entity_id" not in payload
