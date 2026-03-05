import pandas as pd

import pipeline_runner


def test_unearthed_dedupe_prefers_track_url():
    rows = [
        {
            "Artist Name": "Mitch Santiago",
            "Song Title": "Heathrow",
            "Source Directory": "job_unearthed_6",
            "Source URL": "https://abc.net.au/triplejunearthed/track/1",
        },
        {
            "Artist Name": "mitch  santiago ",
            "Song Title": " Heathrow ",
            "Source Directory": "job_unearthed_6",
            "Source URL": "https://abc.net.au/triplejunearthed/track/1?utm=foo",
        },
        {
            "Artist Name": "Mitch Santiago",
            "Song Title": "Heathrow",
            "Source Directory": "job_unearthed_6",
            "Source URL": "",
        },
        {
            "Artist Name": "Mitch Santiago",
            "Song Title": "Heathrow",
            "Source Directory": "job_unearthed_6",
            "Source URL": "https://abc.net.au/triplejunearthed/track/2",
        },
        {
            "Artist Name": "Another Artist",
            "Song Title": "Heathrow",
            "Source Directory": "job_unearthed_6",
            "Source URL": "",
        },
        {
            "Artist Name": "Artist B",
            "Song Title": "Different",
            "Source Directory": "job_unearthed_6",
            "Source URL": "",
        },
    ]

    df = pd.DataFrame(rows)
    deduped = pipeline_runner._dedupe_unearthed_rows(df)

    assert len(deduped.index) == 4
    # Track URL key keeps first occurrence with that URL.
    assert deduped.iloc[0]["Source URL"] == "https://abc.net.au/triplejunearthed/track/1"
    # Url-less duplicate of same artist+song is removed once URL row exists.
    assert "" not in deduped[deduped["Artist Name"].str.contains("Mitch", case=False)]["Source URL"].tolist()
    # Second distinct track URL remains.
    assert "https://abc.net.au/triplejunearthed/track/2" in deduped["Source URL"].tolist()
    # Artist with same song but no track URL remains as distinct artist.
    assert "Another Artist" in deduped["Artist Name"].tolist()


def test_unearthed_dedupe_artist_song_fallback():
    rows = [
        {"Artist Name": "Alpha", "Song Title": "Same Song", "Source Directory": "job_unearthed_7"},
        {"Artist Name": "alpha ", "Song Title": " same   song ", "Source Directory": "job_unearthed_7"},
        {"Artist Name": "Beta", "Song Title": "Same Song", "Source Directory": "job_unearthed_7"},
    ]
    df = pd.DataFrame(rows)
    deduped = pipeline_runner._dedupe_unearthed_rows(df)

    assert len(deduped.index) == 2
    assert {"Alpha", "Beta"} == set(deduped["Artist Name"])


def test_unearthed_dedupe_merges_social_links():
    rows = [
        {
            "Artist Name": "Merge Artist",
            "Song Title": "Merge Song",
            "Source Directory": "job_unearthed_8",
            "Source URL": "https://abc.net.au/triplejunearthed/track/3",
            "Social Link": "https://instagram.com/x",
        },
        {
            "Artist Name": "merge  artist",
            "Song Title": " merge  song ",
            "Source Directory": "job_unearthed_8",
            "Source URL": "https://abc.net.au/triplejunearthed/track/3?utm=foo",
            "Social Link": "https://facebook.com/x",
        },
        {
            "Artist Name": "Merge Artist",
            "Song Title": "Merge Song",
            "Source Directory": "job_unearthed_8",
            "Source URL": "https://abc.net.au/triplejunearthed/track/3#frag",
            "Social Link": "https://tiktok.com/@x",
        },
        {
            "Artist Name": "Another Artist",
            "Song Title": "Merge Song",
            "Source Directory": "job_unearthed_8",
            "Source URL": "",
            "Social Link": "https://instagram.com/another",
        },
        {
            "Artist Name": "Merge Artist",
            "Song Title": "Merge Song",
            "Source Directory": "job_unearthed_8",
            "Source URL": "https://abc.net.au/triplejunearthed/track/4",
            "Social Link": "",
        },
    ]

    df = pd.DataFrame(rows)
    deduped = pipeline_runner._dedupe_unearthed_rows(df)

    # One row for the track key, distinct second track remains.
    assert len(deduped.index) == 3
    social_val = deduped.loc[
        deduped["Source URL"] == "https://abc.net.au/triplejunearthed/track/3", "Social Link"
    ].iloc[0]
    assert social_val == "https://instagram.com/x | https://facebook.com/x | https://tiktok.com/@x"
    # Another artist with same song name survives.
    assert "Another Artist" in deduped["Artist Name"].tolist()
    # Distinct track URL is kept.
    assert "https://abc.net.au/triplejunearthed/track/4" in deduped["Source URL"].tolist()
