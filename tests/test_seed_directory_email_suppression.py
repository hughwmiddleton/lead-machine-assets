import json

import pandas as pd

from email_provenance import EMAIL_PROVENANCE_JSON_COL
from pipeline_runner import _consolidate_email_all


def test_seed_directory_emails_are_stripped_and_not_propagated():
    rows = [
        {
            "Artist Name": "Artist A",
            "Email Source": "Seed directory (site/email scrape)",
            "Email": "seed@example.com",
            "Email_All": "seed@example.com",
        },
        {
            "Artist Name": "Artist B",
            "Email Source": "Seed directory (site/email scrape)",
            "Email": "",
            "Email_All": "",
        },
        {
            "Artist Name": "Artist C",
            "Email Source": "SoundCloud profile",
            "Email": "real@example.com",
            "Email_All": "",
        },
    ]

    df = pd.DataFrame(rows)
    result = _consolidate_email_all(df.copy())

    assert result.loc[0, "Email"] == ""
    assert result.loc[0, "Email_All"] == ""
    assert result.loc[1, "Email"] == ""
    assert result.loc[1, "Email_All"] == ""

    assert result.loc[2, "Email"] == "real@example.com"
    assert result.loc[2, "Email_All"] == "real@example.com"


def test_website_enrich_email_survives_consolidation_and_is_not_stripped():
    """Legitimate artist-owned website contacts must reach canonical fields."""
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Web Artist",
                "Email Source": "Website Enrich",
                "Email": "booking@artist.test",
                "Email_All": "booking@artist.test;info@artist.test",
                "Email_Source_Type": "website_enrich",
                "Email_Source_URL": "https://artist.test/contact",
                "Email_Extract_Method": "regex",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "booking@artist.test": {
                            "source_type": "website_enrich",
                            "surface": "website_contact_page",
                            "source_url": "https://artist.test/contact",
                            "extract_method": "regex",
                        },
                        "info@artist.test": {
                            "source_type": "website_enrich",
                            "surface": "website_homepage",
                            "source_url": "https://artist.test",
                            "extract_method": "regex",
                        },
                    }
                ),
            }
        ]
    )

    result = _consolidate_email_all(df.copy())

    assert result.loc[0, "Email"] == "booking@artist.test"
    assert "booking@artist.test" in result.loc[0, "Email_All"]
    assert "info@artist.test" in result.loc[0, "Email_All"]
    assert result.loc[0, "Email_Source_Type"] == "website_enrich"


def test_website_enrich_does_not_loosen_actual_seed_directory_stripping():
    """Untrusted directory-provided emails must still be stripped."""
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist A",
                "Email Source": "Seed directory (site/email scrape)",
                "Email": "seed@example.com",
                "Email_All": "seed@example.com",
            },
            {
                "Artist Name": "Artist B",
                "Email Source": "Website Enrich",
                "Email": "booking@artist.test",
                "Email_All": "booking@artist.test",
                "Email_Source_Type": "website_enrich",
            },
        ]
    )

    result = _consolidate_email_all(df.copy())

    assert result.loc[0, "Email"] == ""
    assert result.loc[0, "Email_All"] == ""
    assert result.loc[1, "Email"] == "booking@artist.test"
    assert result.loc[1, "Email_All"] == "booking@artist.test"


def test_website_enrich_preserves_existing_stronger_contact_precedence():
    """Facebook-enrich email must remain primary when website email is added later."""
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artistname",
                "Email Source": "Website Enrich",
                "Email": "contact@bandsite.com",
                "Email_All": "contact@bandsite.com;artistname@gmail.com",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "contact@bandsite.com": {
                            "source_type": "website_enrich",
                            "surface": "website_contact_page",
                            "source_url": "https://artistname.test/contact",
                            "extract_method": "regex",
                        },
                        "artistname@gmail.com": {
                            "source_type": "facebook_enrich",
                            "surface": "facebook_about",
                            "source_url": "https://www.facebook.com/artistname/about",
                            "extract_method": "regex",
                        },
                    }
                ),
                "Email_Source_URL": "https://artistname.test/contact",
                "Email_Source_Type": "website_enrich",
            }
        ]
    )

    result = _consolidate_email_all(df.copy())

    assert result.loc[0, "Email"] == "artistname@gmail.com"
    assert "artistname@gmail.com" in result.loc[0, "Email_All"]
    assert "contact@bandsite.com" in result.loc[0, "Email_All"]
    assert result.loc[0, "Email_Source_Type"] == "facebook_enrich"
