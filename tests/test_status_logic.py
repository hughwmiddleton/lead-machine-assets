import final_checker


def _empty_flags():
    return {
        "name_flag": 0,
        "dir_conflict_flag": 0,
        "dup_email_flag": 0,
        "dup_artist_flag": 0,
        "genre_outlier_flag": 0,
    }


def test_normalize_primary_genre_simplifies_noise() -> None:
    raw = 'Podcast / Radio Show / DJ Mix, hipdut, UK, Alt-country'
    assert final_checker.normalize_primary_genre(raw) == "hip-hop, alt-country"


def test_rescue_strong_artist_row_becomes_ok() -> None:
    row = {"Artist Name": "Floetic", "Email": "floetic@example.com"}
    status = final_checker.compute_final_status(row, _empty_flags(), match_score=0.82)
    assert status == "OK"


def test_labelish_rows_stay_blocked_even_with_email() -> None:
    row = {"Artist Name": "Selador Recordings", "Email": "team@selador.com"}
    status = final_checker.compute_final_status(row, _empty_flags(), match_score=0.9)
    assert status == "BLOCK"


def test_unearthed_no_email_rows_stay_ok() -> None:
    row = {
        "Artist Name": "New Unearthed Act",
        "Email": "",
        "Email_All": "",
        "Email_Type": "unearthed_no_emails",
        "Played on Unearthed": "True",
    }
    status = final_checker.compute_final_status(row, _empty_flags(), match_score=0.7)
    assert status == "OK"


def test_staleness_downgrades_old_release() -> None:
    row = {"Release Date": "2020-05-01"}
    downgraded = final_checker.apply_staleness_downgrade("OK", row, enabled=True, fresh_year_cutoff=2023)
    assert downgraded == "WARN"


def test_export_profiles_filter_expected_rows() -> None:
    rows = [
        {"Artist Name": "Solid Lead", "final_status": "OK", "Email": "lead@example.com", "Source Directory": "job_unearthed"},
        {"Artist Name": "Filler Warn", "final_status": "WARN", "Email": "warn@example.com", "Source Directory": "job_bandcamp"},
        {
            "Artist Name": "Unearthed Social",
            "final_status": "OK",
            "Email": "",
            "Email_All": "",
            "Email_Type": "unearthed_no_emails",
            "Source Directory": "job_unearthed_123",
            "Played on Unearthed": "True",
        },
    ]
    safe = final_checker.filter_rows_for_export("studio_safe", rows)
    assert len(safe) == 1
    plus = final_checker.filter_rows_for_export("studio_plus", rows)
    assert len(plus) == 3
    full = final_checker.filter_rows_for_export("full_dump", rows)
    assert len(full) == len(rows)
    unearthed = final_checker.filter_rows_for_export("unearthed_social", rows)
    assert len(unearthed) == 1
    assert unearthed[0]["Artist Name"] == "Unearthed Social"
