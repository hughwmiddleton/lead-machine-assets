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


def test_quarantine_repeated_emails_soundcloud_kept_even_when_late():
    repeated_email = "order_sensitive@example.com"

    bandcamp_rows = [
        {
            "Artist Name": f"bc_artist_{i}",
            "Email": repeated_email,
            "Email_All": repeated_email,
            "Source Directory": "bandcamp/leeds",
            "__source_job": "job_bandcamp_1",
        }
        for i in range(10)
    ]
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

    # Bandcamp rows appear first (master_raw ordering), SoundCloud later.
    df = pd.DataFrame(bandcamp_rows + soundcloud_rows)

    quarantined = quarantine_repeated_emails(df, min_repeats=5)

    sc_mask = quarantined["Source Directory"].str.contains("soundcloud", case=False, na=False)
    bc_mask = quarantined["Source Directory"].str.contains("bandcamp", case=False, na=False)

    # SoundCloud rows should keep their emails even though they appear later.
    assert (quarantined.loc[sc_mask, "Email"] == repeated_email).all()
    assert (quarantined.loc[sc_mask, "Email_All"] == repeated_email).all()

    # Bandcamp rows should be cleared but retain suspects and review flag.
    assert (quarantined.loc[bc_mask, "Email"] == "").all()
    assert (quarantined.loc[bc_mask, "Email_All"] == "").all()
    assert (quarantined.loc[bc_mask, "Suspect_Email"] == repeated_email).all()
    assert (quarantined.loc[bc_mask, "Suspect_Email_All"] == repeated_email).all()
    assert (quarantined.loc[bc_mask, "Needs_Review"] == "TRUE").all()
    assert (quarantined.loc[bc_mask, "Email Source"] == "Quarantined (repeat email)").all()


def test_quarantine_repeated_emails_soundcloud_detected_by_job_when_dir_missing():
    repeated_email = "job-signal-only@example.com"

    bandcamp_rows = [
        {
            "Artist Name": f"bc_artist_{i}",
            "Email": repeated_email,
            "Email_All": repeated_email,
            "Source Directory": "BANDCAMP/LEEDS",
            "__source_job": "job_bandcamp_1",
        }
        for i in range(10)
    ]
    soundcloud_rows = [
        {
            "Artist Name": f"sc_artist_{i}",
            "Email": repeated_email,
            "Email_All": repeated_email,
            "Source Directory": pd.NA if i < 3 else "",
            "__source_job": "job_soundcloud_2",
        }
        for i in range(6)
    ]

    # Bandcamp rows first, SoundCloud rows later; SoundCloud detection must rely on job name.
    df = pd.DataFrame(bandcamp_rows + soundcloud_rows)

    quarantined = quarantine_repeated_emails(df, min_repeats=5)

    sc_mask = quarantined["__source_job"].str.contains("soundcloud", case=False, na=False)
    bc_mask = quarantined["__source_job"].str.contains("bandcamp", case=False, na=False)

    # SoundCloud rows should keep their emails even without Source Directory signal.
    assert (quarantined.loc[sc_mask, "Email"] == repeated_email).all()
    assert (quarantined.loc[sc_mask, "Email_All"] == repeated_email).all()

    # Bandcamp rows should be cleared but retain suspects and review flag.
    assert (quarantined.loc[bc_mask, "Email"] == "").all()
    assert (quarantined.loc[bc_mask, "Email_All"] == "").all()
    assert (quarantined.loc[bc_mask, "Suspect_Email"] == repeated_email).all()
    assert (quarantined.loc[bc_mask, "Suspect_Email_All"] == repeated_email).all()
    assert (quarantined.loc[bc_mask, "Needs_Review"] == "TRUE").all()
    assert (quarantined.loc[bc_mask, "Email Source"] == "Quarantined (repeat email)").all()


def test_quarantine_repeated_emails_handles_pd_na():
    repeated_email = "na-safe@example.com"

    rows = [
        {"Artist Name": f"artist_{i}", "Email": repeated_email, "Email_All": pd.NA, "__source_job": "job_a"}
        for i in range(3)
    ]
    rows += [
        {"Artist Name": f"artist_b_{i}", "Email": pd.NA, "Email_All": repeated_email, "__source_job": "job_b"}
        for i in range(2)
    ]
    rows.append({"Artist Name": "artist_na", "Email": pd.NA, "Email_All": pd.NA, "__source_job": "job_c"})

    df = pd.DataFrame(rows)

    quarantined = quarantine_repeated_emails(df, min_repeats=5)

    assert len(quarantined) == len(df)
