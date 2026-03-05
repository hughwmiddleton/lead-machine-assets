import pandas as pd
import pipeline_runner


def test_run_enrichment_preserves_rows_and_emails_when_capped(monkeypatch, tmp_path):
    # Build 30 rows: 10 unique email rows, 10 exact duplicates of those (should be de-duped for work),
    # and 10 filler rows, with the last two being late email duplicates near the end of the file.
    rows = []
    for i in range(10):
        rows.append(
            {
                "Artist Name": f"Artist{i}",
                "Song Title": "SongA",
                "Source Directory": "soundcloud",
                "Email": f"artist{i}@example.com",
            }
        )

    # Exact duplicates of the first 10 rows (would have been dropped previously)
    rows.extend(rows.copy())

    filler = []
    for i in range(8):
        filler.append(
            {
                "Artist Name": f"Filler{i}",
                "Song Title": "SongB",
                "Source Directory": "soundcloud",
                "Email": "",
            }
        )
    # Late email rows near the end that duplicate earlier artists
    filler.append(
        {
            "Artist Name": "Artist1",
            "Song Title": "SongA",
            "Source Directory": "soundcloud",
            "Email": "artist1@example.com",
        }
    )
    filler.append(
        {
            "Artist Name": "Artist2",
            "Song Title": "SongA",
            "Source Directory": "soundcloud",
            "Email": "artist2@example.com",
        }
    )
    rows.extend(filler)

    assert len(rows) == 30

    input_csv = tmp_path / "master_post_fb.csv"
    output_csv = tmp_path / "master_final.csv"
    pd.DataFrame(rows).to_csv(input_csv, index=False)

    # Simulate smoke cap environment present in repro steps.
    monkeypatch.setenv("SMOKE_SEED_CAP", "20")

    pipeline_runner.run_enrichment(input_csv.as_posix(), output_csv.as_posix(), logger=None, night_mode=True)

    df_out = pd.read_csv(output_csv)
    assert len(df_out) == len(rows)

    # The late email rows (duplicate artists near the end) must still be present.
    late_artists = df_out[df_out["Artist Name"].isin(["Artist1", "Artist2"])]
    assert len(late_artists) >= 3  # both early + late duplicates should survive
    assert "artist1@example.com" in set(late_artists["Email"].str.lower())
    assert "artist2@example.com" in set(late_artists["Email"].str.lower())
