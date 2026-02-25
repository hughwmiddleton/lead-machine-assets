import pandas as pd

from night_mode_fb import NightModeFacebookEnricher, NightModeFacebookResult, _fb_status_is_rejected
from pipeline_runner import _build_final_export_frame


def test_night_fb_reject_guard_blocks_email_application():
    logs: list[str] = []
    enricher = NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=logs.append,
        use_shared_session=False,
    )
    row = {"FB_Status": "reject:name_mismatch", "Artist Name": "Test Artist", "Email": "", "Email_All": ""}
    night_result = NightModeFacebookResult()
    night_result.email = "fb@test.com"
    night_result.email_all = "fb@test.com"
    night_result.email_type = "fb_night"
    night_result.facebook_url = "https://facebook.com/page"

    updated = enricher._apply_night_fb_result(row, night_result, ["fb@test.com"], "https://facebook.com/page")

    assert updated.get("Email") == ""
    assert updated.get("Email_All") == ""
    assert "__fb_emails_applied" not in updated
    assert any("Discarding emails from rejected FB page" in msg for msg in logs)


def test_night_fb_accept_tracks_applied_emails():
    enricher = NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    row = {"FB_Status": "", "Artist Name": "Test Artist", "Email": "", "Email_All": ""}
    night_result = NightModeFacebookResult()
    night_result.email = "fb@test.com"
    night_result.email_all = "fb@test.com"
    night_result.email_type = "fb_night"
    night_result.facebook_url = "https://facebook.com/page"

    updated = enricher._apply_night_fb_result(row, night_result, ["fb@test.com", "FB@Test.com"], "https://facebook.com/page")

    assert updated.get("Email") == "fb@test.com"
    assert updated.get("Email_All") == "fb@test.com"
    assert updated.get("__fb_emails_applied") == "fb@test.com"
    assert updated.get("FB_Status") == "ok"


def test_export_strips_rejected_fb_emails_only():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist A",
                "final_status": "OK",
                "FB_Status": "blocked_403",
                "__fb_emails_applied": "fb@test.com",
                "Email": "fb@test.com",
                "Email_All": "fb@test.com;other@test.com",
                "Location": "",
                "Country_Derived": "",
                "Social Link": "",
                "SoundCloud Link": "",
                "External Links": "",
                "Spotify_URL": "",
            }
        ]
    )

    export_df = _build_final_export_frame(df)
    row = export_df.iloc[0]

    assert row["Primary Email"] == "other@test.com"
    assert "fb@test.com" not in row["All Emails"]


def test_fb_status_is_rejected_helper():
    assert _fb_status_is_rejected("reject:name_mismatch")
    assert _fb_status_is_rejected("blocked_403")
    assert not _fb_status_is_rejected("ok")
