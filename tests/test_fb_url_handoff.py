import pandas as pd
import pytest

import night_mode_runner
import pipeline_runner
from night_mode_fb import NightFBRunState, NightFBSessionSource


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


class _StaticNightFBHelper:
    def __init__(self, payload):
        self.payload = payload
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_session_failure(self):
        return False, ""

    def get_pass_a_counts(self):
        return {}

    def get_email_stats(self):
        return {}

    def enrich_row_with_facebook_night(self, row, row_index=0):
        self.rows.append({"row": dict(row or {}), "row_index": row_index})
        return dict(self.payload or {})


def test_night_fb_promotes_final_reloaded_dataframe_before_intake(monkeypatch, tmp_path) -> None:
    rows = [
        {
            "Artist Name": "Share Artist",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "facebook_url": "",
            "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr, https://www.instagram.com/shareartist",
            "External Links": "",
        },
        {
            "Artist Name": "Direct Artist",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "https://www.facebook.com/directartist",
            "Facebook URL": "",
            "facebook_url": "",
            "Social Link": "",
            "External Links": "",
        },
        {
            "Artist Name": "Non FB Artist",
            "Email": "",
            "Email_All": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "facebook_url": "",
            "Social Link": "https://www.instagram.com/nonfbartist",
            "External Links": "",
        },
    ]
    input_csv = tmp_path / "master_pre_fb.csv"
    output_csv = tmp_path / "master_post_fb.csv"
    state_path = tmp_path / "facebook_state.json"
    pd.DataFrame(rows, dtype=str).to_csv(input_csv, index=False)

    helper = _StaticNightFBHelper(
        {
            "FB_Status": "pass_a_no_email_on_page",
            "FB_Attempt_State": "attempted_fb_no_email_on_page",
        }
    )
    logged = []

    monkeypatch.setenv("FB_USERNAME", "user")
    monkeypatch.setenv("FB_PASSWORD", "pass")
    monkeypatch.setattr(pipeline_runner, "NightModeFacebookEnricher", lambda *args, **kwargs: helper)
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: type("M", (), {"scrape_csv": lambda *a, **k: None})())
    monkeypatch.setattr(pipeline_runner, "_load_fb_state", lambda _: {})
    monkeypatch.setattr(pipeline_runner, "_write_fb_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pipeline_runner,
        "_build_night_fb_share_promotion_resolver",
        lambda **kwargs: (lambda raw: "https://web.facebook.com/artistpage/?mibextid=wwXIfr"),
    )

    run_state = NightFBRunState(
        session_source=NightFBSessionSource(
            mode="credentials",
            reason="test",
            can_probe=True,
            has_credentials=True,
        )
    )

    pipeline_runner.run_facebook_global_pass_nightmode(
        input_csv=input_csv.as_posix(),
        output_csv=output_csv.as_posix(),
        state_path=state_path.as_posix(),
        max_rows_per_run=100,
        per_row_delay_range=(0.0, 0.0),
        short_break_every=0,
        long_break_every=0,
        logger=logged.append,
        skip_rows_with_email=True,
        night_fb_run_state=run_state,
    )

    out = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")
    share_row = out.loc[out["Artist Name"] == "Share Artist"].iloc[0]
    direct_row = out.loc[out["Artist Name"] == "Direct Artist"].iloc[0]
    non_fb_row = out.loc[out["Artist Name"] == "Non FB Artist"].iloc[0]

    assert share_row["Facebook_URL"] == "https://www.facebook.com/artistpage"
    assert share_row["Facebook URL"] == "https://www.facebook.com/artistpage"
    assert share_row["facebook_url"] == "https://www.facebook.com/artistpage"
    assert direct_row["Facebook_URL"] == "https://www.facebook.com/directartist"
    assert non_fb_row["Facebook_URL"] == ""

    share_payload = next(item["row"] for item in helper.rows if item["row"]["Artist Name"] == "Share Artist")
    direct_payload = next(item["row"] for item in helper.rows if item["row"]["Artist Name"] == "Direct Artist")
    assert share_payload["Facebook_URL"] == "https://www.facebook.com/artistpage"
    assert direct_payload["Facebook_URL"] == "https://www.facebook.com/directartist"
    assert not any(item["row"]["Artist Name"] == "Non FB Artist" for item in helper.rows)

    handoff_logs = [msg for msg in logged if "[Night FB][Promotion Handoff]" in msg]
    assert handoff_logs
    assert "identity_ok=1" in handoff_logs[-1]
    assert "canonical_field_present=2" in handoff_logs[-1]
    assert any("[FB Share Canonicalize]" in msg and "artist='Share Artist'" in msg and "detected=1" in msg for msg in logged)
    assert any("[FB Share Canonicalize]" in msg and "artist='Share Artist'" in msg and "outcome='resolved'" in msg for msg in logged)
    assert any("artist='Share Artist'" in msg and "fb_url_present=True" in msg for msg in logged)
    assert any("artist='Direct Artist'" in msg and "fb_url_present=True" in msg for msg in logged)
