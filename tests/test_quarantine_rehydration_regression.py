import pandas as pd

from night_mode_runner import quarantine_repeated_emails
from pipeline_runner import _consolidate_email_all


def test_same_job_bandcamp_repeats_quarantined():
    repeated_email = "repeat@example.com"
    rows = [
        {
            "Artist Name": f"artist_{i}",
            "Email": repeated_email,
            "Email_All": repeated_email,
            "Directory_Email": repeated_email,
            "__source_job": "job_bandcamp",
            "Source Directory": "bandcamp/leeds",
        }
        for i in range(5)
    ]

    df = pd.DataFrame(rows)
    quarantined = quarantine_repeated_emails(df, min_repeats=5)

    assert (quarantined["Email"].value_counts().get(repeated_email, 0)) == 1
    assert (quarantined["Email_All"].value_counts().get(repeated_email, 0)) == 1

    cleared_mask = quarantined["Email"] == ""
    assert cleared_mask.sum() == 4
    assert (quarantined.loc[cleared_mask, "Email_All"] == "").all()
    assert (quarantined.loc[cleared_mask, "Directory_Email"] == "").all()
    assert (quarantined.loc[cleared_mask, "Needs_Review"] == "TRUE").all()
    assert (quarantined.loc[cleared_mask, "Email Source"] == "Quarantined (repeat email)").all()


def test_consolidate_email_all_respects_quarantine():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "quarantined",
                "Email": "",
                "Email_All": "",
                "Directory_Email": "rehydrate@example.com",
                "Unearthed_Email": "rehydrate@example.com",
                "Email Source": "Quarantined (repeat email)",
                "Needs_Review": "TRUE",
                "Suspect_Email_All": "rehydrate@example.com",
            },
            {
                "Artist Name": "normal",
                "Email": "",
                "Email_All": "",
                "Directory_Email": "keepme@example.com",
                "Unearthed_Email": "",
                "Email Source": "Directory scrape",
                "Needs_Review": "FALSE",
                "Suspect_Email_All": "",
            },
        ]
    )

    consolidated = _consolidate_email_all(df.copy())

    quarantined_row = consolidated.iloc[0]
    assert quarantined_row["Email_All"] == ""

    normal_row = consolidated.iloc[1]
    assert normal_row["Email_All"] == "keepme@example.com"


def test_consolidate_respects_quarantine_with_email_address_column():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "quarantined",
                "Email": "",
                "Email_All": "",
                "Email Address": "rehydrate@example.com",
                "Email Source": "Quarantined (repeat email)",
                "Needs_Review": "TRUE",
                "Suspect_Email_All": "rehydrate@example.com",
            },
            {
                "Artist Name": "normal",
                "Email": "",
                "Email_All": "",
                "Email Address": "keepme@example.com",
                "Email Source": "Directory scrape",
                "Needs_Review": "FALSE",
                "Suspect_Email_All": "",
            },
        ]
    )

    consolidated = _consolidate_email_all(df.copy())

    quarantined_row = consolidated.iloc[0]
    assert quarantined_row["Email_All"] == ""

    normal_row = consolidated.iloc[1]
    assert normal_row["Email_All"] == "keepme@example.com"
