import pandas as pd

from night_mode_runner import quarantine_repeated_emails


def test_quarantine_repeated_emails_soundcloud_kept_bandcamp_cleared():
    repeated_email = "japan.entertainment.home@gmail.com"

    soundcloud_rows = [
        {
            "Artist Name": f"sc_artist_{i}",
            "Email": repeated_email,
            "Email_All": repeated_email,
            "Source Directory": "soundcloud",
            "__source_job": "job_soundcloud",
        }
        for i in range(6)
    ]
    bandcamp_rows = [
        {
            "Artist Name": f"bc_artist_{i}",
            "Email": repeated_email,
            "Email_All": repeated_email,
            "Source Directory": "bandcamp/leeds",
            "__source_job": "job_bandcamp",
        }
        for i in range(10)
    ]

    df = pd.DataFrame(soundcloud_rows + bandcamp_rows)

    quarantined = quarantine_repeated_emails(df, min_repeats=5)

    sc_mask = quarantined["Source Directory"].str.contains("soundcloud", case=False, na=False)
    bc_mask = quarantined["Source Directory"].str.contains("bandcamp", case=False, na=False)

    # SoundCloud rows should keep their emails.
    assert (quarantined.loc[sc_mask, "Email"] == repeated_email).all()
    assert (quarantined.loc[sc_mask, "Email_All"] == repeated_email).all()

    # Bandcamp rows should be cleared but retain suspects and review flag.
    assert (quarantined.loc[bc_mask, "Email"] == "").all()
    assert (quarantined.loc[bc_mask, "Email_All"] == "").all()
    assert (quarantined.loc[bc_mask, "Suspect_Email"] == repeated_email).all()
    assert (quarantined.loc[bc_mask, "Suspect_Email_All"] == repeated_email).all()
    assert (quarantined.loc[bc_mask, "Needs_Review"] == "TRUE").all()
    assert (quarantined.loc[bc_mask, "Email Source"] == "Quarantined (repeat email)").all()
