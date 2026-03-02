import pandas as pd

from pipeline_runner import recompute_final_status_post_enrichment


def _make_df(row: dict) -> pd.DataFrame:
    return pd.DataFrame([row])


def test_downgrades_origin_based_block_with_valid_email():
    df = _make_df(
        {
            "Artist Name": "Tand",
            "final_status": "BLOCK",
            "origin_match_flag": 0,
            "directory_conflict_flag": 1,
            "duplicate_email_flag": 0,
            "duplicate_artist_flag": 0,
            "name_consistency_flag": 1,
            "Email": "info@tandtheband.com",
            "Email_All": "",
        }
    )

    result = recompute_final_status_post_enrichment(df.copy())

    row = result.iloc[0]
    assert row["final_status"] == "WARN"
    assert str(row["Needs_Review"]).upper() == "TRUE"
    assert row["FB_Review_Reason"] == "origin_mismatch_downgraded"


def test_duplicate_email_remains_block():
    df = _make_df(
        {
            "Artist Name": "Duped",
            "final_status": "BLOCK",
            "origin_match_flag": 0,
            "directory_conflict_flag": 1,
            "duplicate_email_flag": 1,
            "duplicate_artist_flag": 0,
            "name_consistency_flag": 1,
            "Email": "dupe@example.com",
        }
    )

    result = recompute_final_status_post_enrichment(df.copy())

    row = result.iloc[0]
    assert row["final_status"] == "BLOCK"
    assert str(row.get("Needs_Review", "")).upper() != "TRUE"


def test_missing_email_remains_block():
    df = _make_df(
        {
            "Artist Name": "No Email",
            "final_status": "BLOCK",
            "origin_match_flag": 0,
            "directory_conflict_flag": 1,
            "duplicate_email_flag": 0,
            "duplicate_artist_flag": 0,
            "name_consistency_flag": 1,
            "Email": "",
            "Email_All": "",
        }
    )

    result = recompute_final_status_post_enrichment(df.copy())

    row = result.iloc[0]
    assert row["final_status"] == "BLOCK"


def test_short_name_domain_match_allows_downgrade():
    df = _make_df(
        {
            "Artist Name": "Tand",
            "final_status": "BLOCK",
            "origin_match_flag": 1,  # simulate origin ok but directory conflict triggered
            "directory_conflict_flag": 1,
            "duplicate_email_flag": 0,
            "duplicate_artist_flag": 0,
            "name_consistency_flag": 1,
            "Email": "hello@tandmusic.com",
            "Email_All": "",
        }
    )

    result = recompute_final_status_post_enrichment(df.copy())

    row = result.iloc[0]
    assert row["final_status"] == "WARN"
    assert str(row["Needs_Review"]).upper() == "TRUE"


def test_post_enrich_respects_fb_reject_token():
    df = _make_df(
        {
            "Artist Name": "FB Reject",
            "final_status": "BLOCK",
            "origin_match_flag": 0,
            "directory_conflict_flag": 1,
            "duplicate_email_flag": 0,
            "duplicate_artist_flag": 0,
            "name_consistency_flag": 1,
            "Email": "ok@example.com",
            "Email_All": "ok@example.com",
            "FB_Status": "rejected_candidate",
        }
    )

    result = recompute_final_status_post_enrichment(df.copy())

    row = result.iloc[0]
    assert row["final_status"] == "BLOCK"
