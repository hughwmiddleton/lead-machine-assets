import pandas as pd
import pytest

import night_mode_runner
import pipeline_runner


def test_facebook_url_survives_raw_to_pre_fb_handoff(tmp_path) -> None:
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Lower Alias",
                "facebook_url": "https://www.facebook.com/existinglower",
                "Facebook_URL": "",
                "Social Link": "",
                "External Links": "",
            },
            {
                "Artist Name": "Mixed Social",
                "facebook_url": "",
                "Facebook_URL": "",
                "Social Link": "https://www.facebook.com/georgerileymusic, https://www.instagram.com/georgeriley___",
                "External Links": "",
            },
            {
                "Artist Name": "Placeholder",
                "facebook_url": "facebook.com/nan",
                "Facebook_URL": "",
                "Social Link": "",
                "External Links": "",
            },
        ],
        dtype=str,
    ).fillna("")

    master_raw = night_mode_runner._promote_fb_urls_df(seed_df.copy())
    raw_path = tmp_path / "master_raw.csv"
    master_raw.to_csv(raw_path, index=False)

    master_enriched = pd.read_csv(raw_path, dtype=str, keep_default_na=False).fillna("")
    master_enriched = pipeline_runner._promote_fb_urls_df(master_enriched)
    enriched_path = tmp_path / "master_enriched.csv"
    master_enriched.to_csv(enriched_path, index=False)

    master_pre_fb = pd.read_csv(enriched_path, dtype=str, keep_default_na=False).fillna("")
    master_pre_fb = pipeline_runner._promote_fb_urls_df(master_pre_fb)

    lower_alias_row = master_pre_fb.loc[master_pre_fb["Artist Name"] == "Lower Alias"].iloc[0]
    mixed_social_row = master_pre_fb.loc[master_pre_fb["Artist Name"] == "Mixed Social"].iloc[0]
    placeholder_row = master_pre_fb.loc[master_pre_fb["Artist Name"] == "Placeholder"].iloc[0]

    assert lower_alias_row["Facebook_URL"] == "https://www.facebook.com/existinglower"
    assert mixed_social_row["Facebook_URL"] == "https://www.facebook.com/georgerileymusic"
    assert placeholder_row["Facebook_URL"] == ""


def test_payload_promoted_facebook_url_survives_raw_to_pre_fb_handoff(tmp_path) -> None:
    pytest.importorskip("PyQt5")
    import cross_directory_enricher as cde

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Payload Promoted",
                "facebook_url": "",
                "Facebook_URL": "",
                "Facebook URL": "",
                "Social Link": "",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")

    payload = cde.EnrichmentPayload(
        socials={"https://fb.com/payloadpromoted"},
        websites=set(),
        source_dir="soundcloud",
        source_url="https://soundcloud.com/payloadpromoted",
        match_score=0.9,
    )
    assert cde._promote_payload_facebook_url(seed_df, 0, payload) is True

    master_raw = night_mode_runner._promote_fb_urls_df(seed_df.copy())
    raw_path = tmp_path / "master_raw.csv"
    master_raw.to_csv(raw_path, index=False)

    master_enriched = pd.read_csv(raw_path, dtype=str, keep_default_na=False).fillna("")
    master_enriched = pipeline_runner._promote_fb_urls_df(master_enriched)
    enriched_path = tmp_path / "master_enriched.csv"
    master_enriched.to_csv(enriched_path, index=False)

    master_pre_fb = pd.read_csv(enriched_path, dtype=str, keep_default_na=False).fillna("")
    master_pre_fb = pipeline_runner._promote_fb_urls_df(master_pre_fb)

    promoted_row = master_pre_fb.loc[master_pre_fb["Artist Name"] == "Payload Promoted"].iloc[0]

    assert promoted_row["Facebook_URL"] == "https://www.facebook.com/payloadpromoted"
