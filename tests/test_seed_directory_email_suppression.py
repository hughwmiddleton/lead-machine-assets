import pandas as pd

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
