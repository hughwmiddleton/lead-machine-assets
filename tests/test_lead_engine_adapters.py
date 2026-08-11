from copy import deepcopy

import pandas as pd
import pytest

from lead_engine import lead_record_from_row, source_occurrence_from_row


@pytest.mark.parametrize(
    "row",
    [
        {
            "Artist Name": "Alias Artist",
            "Source Directory": "spotify",
            "Source URL": "https://open.spotify.com/artist/Alias123",
            "final_status": "PASS",
        },
        {
            "Artist": "Alias Artist",
            "Source_Directory": "spotify",
            "Source_URL": "https://open.spotify.com/artist/Alias123",
            "Final_Status": "PASS",
        },
        {
            "Band Name": "Alias Artist",
            "Discovery Source": "spotify",
            "Profile URL": "https://open.spotify.com/artist/Alias123",
            "Final Status": "PASS",
        },
    ],
)
def test_documented_alias_variants_convert_to_same_contract_identity(row):
    occurrence = source_occurrence_from_row(row)
    record = lead_record_from_row(row)
    assert occurrence.identity_kind == "spotify_artist_id"
    assert record.final_status == "PASS"


def test_adapter_does_not_mutate_input_row():
    row = {
        "Artist Name": "Untouched Artist",
        "Source_Directory": "bandcamp",
        "Source_URL": "https://untouched.bandcamp.com/",
        "Needs_Review": "yes",
    }
    before = deepcopy(row)
    lead_record_from_row(row)
    assert row == before


def test_run_job_and_discovery_metadata_are_preserved_but_not_identity_inputs():
    base = {
        "Artist Name": "Metadata Artist",
        "Source Directory": "Triple J Unearthed",
        "Source URL": "https://www.abc.net.au/triplejunearthed/artist/metadata-artist",
        "Date Added": "2026-01-01",
        "Run ID": "run-one",
        "Job ID": "job-one",
        "Discovery Source": "Unearthed music index",
    }
    changed = {
        **base,
        "Date Added": "2026-08-11",
        "Run ID": "run-two",
        "Job ID": "job-two",
    }
    first = source_occurrence_from_row(base)
    second = source_occurrence_from_row(changed)
    assert first.source_occurrence_id == second.source_occurrence_id
    assert first.discovered_at == "2026-01-01"
    assert first.run_id == "run-one"
    assert first.job_id == "job-one"
    assert first.source_name == "Unearthed music index"


def test_normalized_identity_and_raw_evidence_coexist():
    record = lead_record_from_row(
        {
            "Artist Name": "  CAFÉ   NOIR  ",
            "Location": "  Montréal  ",
            "Source Directory": "local_seed",
        }
    )
    assert record.artist_name_normalized == "café noir"
    assert record.location_normalized == "montréal"
    assert next(item.value for item in record.provenance if item.fact_name == "artist_name") == "  CAFÉ   NOIR  "
    assert next(item.value for item in record.provenance if item.fact_name == "location") == "  Montréal  "


def test_adapter_accepts_pandas_series_and_treats_pd_na_as_missing():
    row = pd.Series(
        {
            "Artist Name": "Series Artist",
            "Source_Directory": "spotify",
            "Spotify_Artist_ID": "Series123",
            "Location": pd.NA,
            "Final_Status": pd.NA,
        }
    )
    record = lead_record_from_row(row)
    assert record.artist_name_normalized == "series artist"
    assert record.location_normalized == ""
    assert record.final_status == ""


def test_unknown_url_does_not_fabricate_a_source_type():
    occurrence = source_occurrence_from_row(
        {"Artist Name": "Unknown Source Artist", "Source URL": "https://directory.example/artists/42"}
    )
    assert occurrence.source_type == ""
    assert occurrence.source_occurrence_id.startswith("le:source-occurrence:v1:unspecified:")
