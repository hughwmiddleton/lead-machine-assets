import pandas as pd

from pipeline_runner import export_master_leads, recompute_final_status_post_enrichment


def _make_df(row: dict) -> pd.DataFrame:
    return pd.DataFrame([row])


def _base_success_row(**overrides: str) -> dict[str, str]:
    row = {
        "Artist Name": "Status Test",
        "final_status": "WARN",
        "match_score_overall": "0.95",
        "name_consistency_flag": "1.0",
        "name_consistency_flag_polarity": "consistent_is_1",
        "directory_conflict_flag": "0.0",
        "duplicate_email_flag": "0.0",
        "duplicate_artist_flag": "0.0",
        "genre_outlier_flag": "0.0",
        "Email": "hello@statustest.com",
        "Email_All": "hello@statustest.com",
        "Email_Source_URL": "https://statustest.com/contact",
        "Email_Source_Type": "website_enrich",
        "Email_Extract_Method": "regex",
        "FB_Status": "",
        "FB_Selected_By": "",
        "FB_Match_Level": "",
        "FB_Name_Consistency_Flag": "",
        "FB_Review_Reason": "",
        "Source Directory": "website",
        "Source URL": "https://statustest.com",
        "Review_Urls": "",
    }
    row.update(overrides)
    return row


def test_promotes_stale_warn_clean_non_fb_success_to_ok():
    df = _make_df(_base_success_row())

    result = recompute_final_status_post_enrichment(df.copy())

    row = result.iloc[0]
    assert row["final_status"] == "OK"


def test_promotes_stale_warn_clean_fb_success_to_ok():
    df = _make_df(
        _base_success_row(
            Email="booking@statustest.com",
            Email_All="booking@statustest.com",
            Email_Source_URL="https://www.facebook.com/statustest/about",
            Email_Source_Type="facebook_enrich",
            FB_Status="ok",
            FB_Selected_By="direct_match",
            FB_Match_Level="exact",
            FB_Name_Consistency_Flag="1.0",
        )
    )

    result = recompute_final_status_post_enrichment(df.copy())

    row = result.iloc[0]
    assert row["final_status"] == "OK"


def test_stale_warn_low_confidence_fb_row_remains_warn():
    df = _make_df(
        _base_success_row(
            Email="booking@statustest.com",
            Email_All="booking@statustest.com",
            Email_Source_URL="https://www.facebook.com/statustest/about",
            Email_Source_Type="facebook_enrich",
            FB_Status="ok",
            FB_Selected_By="mismatch_fallback",
            FB_Match_Level="mismatch",
            FB_Name_Consistency_Flag="0.0",
            FB_Review_Reason="fb_low_confidence_match",
        )
    )

    result = recompute_final_status_post_enrichment(df.copy())

    row = result.iloc[0]
    assert row["final_status"] == "WARN"


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
            "name_consistency_flag_polarity": "consistent_is_1",
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
            "name_consistency_flag_polarity": "consistent_is_1",
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
            "name_consistency_flag_polarity": "consistent_is_1",
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
            "name_consistency_flag_polarity": "consistent_is_1",
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
            "name_consistency_flag_polarity": "consistent_is_1",
            "Email": "ok@example.com",
            "Email_All": "ok@example.com",
            "FB_Status": "rejected_candidate",
        }
    )

    result = recompute_final_status_post_enrichment(df.copy())

    row = result.iloc[0]
    assert row["final_status"] == "BLOCK"


def test_export_master_leads_recomputes_stale_warn_status_before_writing(tmp_path):
    input_path = tmp_path / "master_enriched.csv"
    output_path = tmp_path / "master_export_leads.csv"
    pd.DataFrame([_base_success_row()]).to_csv(input_path, index=False)

    export_master_leads(str(input_path), str(output_path), export_profile="full_dump")

    exported = pd.read_csv(output_path, dtype=str, keep_default_na=False)
    assert exported.iloc[0]["final_status"] == "OK"
