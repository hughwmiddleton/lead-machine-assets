import json

import pandas as pd

from email_provenance import EMAIL_PROVENANCE_JSON_COL
from pipeline_runner import _build_final_export_frame, _derive_primary_email, _select_primary_email_for_row


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
                Email="noreply@artistlabel.com",
                Email_All="noreply@artistlabel.com;booking@artist.com;press@artistlabel.com",
                Email_Source_URL="",
                Email_Source_Type="",
                Email_Extract_Method="",
            )
        ]
    )

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Primary Email"] == "booking@artist.com"
    assert (
        export_df.iloc[0]["All Emails"]
        == "booking@artist.com;press@artistlabel.com;noreply@artistlabel.com"
    )


def test_final_export_uses_selected_email_provenance_instead_of_stale_row_fields() -> None:
    df = pd.DataFrame(
        [
            _build_export_row(
                Email="to@nomograph.mastering",
                Email_All="to@nomograph.mastering;admin@artist.test",
                Email_Source_URL="https://nomograph.mastering/contact",
                Email_Source_Type="website_enrich",
                Email_Extract_Method="regex",
                Spotify_Website_URL="https://artist.test",
                **{
                    EMAIL_PROVENANCE_JSON_COL: json.dumps(
                        {
                            "to@nomograph.mastering": {
                                "source_type": "website_enrich",
                                "surface": "website_contact_page",
                                "source_url": "https://nomograph.mastering/contact",
                                "extract_method": "regex",
                            },
                            "admin@artist.test": {
                                "source_type": "facebook_enrich",
                                "surface": "facebook_about",
                                "source_url": "https://www.facebook.com/artist/about",
                                "extract_method": "regex",
                            },
                        }
                    )
                },
            )
        ]
    )

    export_df = _build_final_export_frame(df)

    assert export_df.iloc[0]["Primary Email"] == "admin@artist.test"
    assert export_df.iloc[0]["Email Source"] == "Facebook About"
    assert export_df.iloc[0]["Email_Source_URL"] == "https://www.facebook.com/artist/about"
    assert export_df.iloc[0]["Needs_Review"] == "FALSE"


def test_final_export_matches_row_level_primary_email_selection_for_identity_tiebreak() -> None:
    row = _build_export_row(
        Email="for@faridani.co",
        Email_All="for@faridani.co;jazzypdale@gmail.com",
        **{
            "Artist Name": "Jazzy Dale",
            "SoundCloud Link": "https://soundcloud.com/jazzypdale",
            EMAIL_PROVENANCE_JSON_COL: json.dumps(
                {
                    "for@faridani.co": {
                        "source_type": "soundcloud_live",
                        "surface": "soundcloud_profile",
                        "source_url": "https://soundcloud.com/jazzypdale",
                        "extract_method": "regex",
                    },
                    "jazzypdale@gmail.com": {
                        "source_type": "soundcloud_live",
                        "surface": "soundcloud_profile",
                        "source_url": "https://soundcloud.com/jazzypdale",
                        "extract_method": "regex",
                    },
                }
            ),
        },
    )
    expected_primary, _ = _select_primary_email_for_row(row, row["Email"], row["Email_All"])

    assert _derive_primary_email(row["Email"], row["Email_All"], row) == expected_primary

    export_df = _build_final_export_frame(pd.DataFrame([row]))

    assert export_df.iloc[0]["Primary Email"] == expected_primary
    assert export_df.iloc[0]["All Emails"] == "jazzypdale@gmail.com;for@faridani.co"
