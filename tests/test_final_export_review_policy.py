import pandas as pd

from pipeline_runner import _build_final_export_frame


def _build_export_row(**overrides: str) -> dict[str, str]:
    row = {
        "Artist Name": "Test Artist",
        "Song Title": "Test Song",
        "final_status": "OK",
        "Email": "hello@testartist.com",
        "Email_All": "hello@testartist.com",
        "Needs_Review": "FALSE",
        "Email_Source_URL": "https://testartist.com/contact",
        "Email_Source_Type": "website_enrich",
        "Email_Extract_Method": "mailto",
        "FB_Status": "",
        "Review_Urls": "",
        "FB_Review_Reason": "",
        "Location": "",
        "Country_Derived": "",
        "Social Link": "",
        "SoundCloud Link": "",
        "External Links": "",
        "Spotify_URL": "",
        "Source URL": "https://testartist.com",
        "Source Directory": "website",
    }
    row.update(overrides)
    return row


def test_final_export_auto_approves_explicit_facebook_email() -> None:
    df = pd.DataFrame(
        [
            _build_export_row(
                Email="booking@testartist.com",
                Email_All="booking@testartist.com",
                Email_Source_URL="https://www.facebook.com/testartist/about",
                Email_Source_Type="facebook_enrich",
                Email_Extract_Method="regex",
                FB_Status="ok",
            )
        ]
    )

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Email Source"] == "Facebook About"
    assert export_df.iloc[0]["Needs_Review"] == "FALSE"


def test_final_export_auto_approves_website_email_with_provenance() -> None:
    df = pd.DataFrame([_build_export_row()])

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Email Source"] == "Website"
    assert export_df.iloc[0]["Needs_Review"] == "FALSE"


def test_final_export_marks_suspicious_system_email_for_review() -> None:
    df = pd.DataFrame(
        [
            _build_export_row(
                Email="alerts@sentry.io",
                Email_All="alerts@sentry.io",
            )
        ]
    )

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Needs_Review"] == "TRUE"


def test_final_export_auto_approves_warn_rows_with_explicit_facebook_provenance() -> None:
    df = pd.DataFrame(
        [
            _build_export_row(
                final_status="WARN",
                Email="booking@testartist.com",
                Email_All="booking@testartist.com",
                Email_Source_URL="https://www.facebook.com/testartist/about",
                Email_Source_Type="facebook_enrich",
                Email_Extract_Method="regex",
                FB_Status="ok",
            )
        ]
    )

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Email Source"] == "Facebook About"
    assert export_df.iloc[0]["Needs_Review"] == "FALSE"


def test_final_export_auto_approves_warn_rows_with_website_provenance() -> None:
    df = pd.DataFrame([_build_export_row(final_status="WARN")])

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Email Source"] == "Website"
    assert export_df.iloc[0]["Needs_Review"] == "FALSE"


def test_final_export_keeps_warn_rows_with_weak_provenance_under_review() -> None:
    df = pd.DataFrame(
        [
            _build_export_row(
                final_status="WARN",
                Email_Source_Type="",
                Email_Source_URL="https://testartist.com/contact",
                **{
                    "Source URL": "https://soundcloud.com/testartist",
                    "Source Directory": "soundcloud",
                },
            )
        ]
    )

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Needs_Review"] == "TRUE"


def test_final_export_keeps_block_rows_under_review_even_with_explicit_provenance() -> None:
    df = pd.DataFrame([_build_export_row(final_status="BLOCK")])

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Needs_Review"] == "TRUE"


def test_final_export_requires_provenance_for_ok_rows() -> None:
    df = pd.DataFrame([_build_export_row(Email_Source_URL="")])

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Needs_Review"] == "TRUE"


def test_final_export_preserves_upstream_review_flag() -> None:
    df = pd.DataFrame([_build_export_row(Needs_Review="TRUE")])

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Needs_Review"] == "TRUE"


def test_final_export_prefers_ranked_primary_over_weaker_existing_email() -> None:
    df = pd.DataFrame(
        [
            _build_export_row(
                Email="support@bandcamp.com",
                Email_All="support@bandcamp.com;booking@artist.com;press@artistlabel.com",
            )
        ]
    )

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Primary Email"] == "booking@artist.com"
    assert (
        export_df.iloc[0]["All Emails"]
        == "booking@artist.com;press@artistlabel.com;support@bandcamp.com"
    )
