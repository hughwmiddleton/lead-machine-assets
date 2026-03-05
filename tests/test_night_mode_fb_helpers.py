import pandas as pd

import night_mode_fb
from origin_validator import dedupe_pre_auto_validate


def test_extract_emails_and_primary_selection() -> None:
    html = """
    <html>
      <body>
        <a href="mailto:bookings@zuso.com">Email</a>
        <div>Contact: ericaavenue@gmail.com</div>
        <div>alt: info@label.com</div>
      </body>
    </html>
    """
    emails, used_mailto = night_mode_fb._extract_emails_from_html(html)
    assert "ericaavenue@gmail.com" in emails
    assert "info@label.com" in emails
    assert used_mailto is True
    primary = night_mode_fb._choose_primary_email(emails, "ericaavenue")
    assert primary == "ericaavenue@gmail.com"
    merged = night_mode_fb._merge_email_all("bookings@zuso.com", emails)
    assert "bookings@zuso.com" in merged.split(";")
    assert len(set(merged.split(";"))) == len(merged.split(";"))


def test_dedupe_prefers_fb_in_night_mode() -> None:
    df = pd.DataFrame(
        [
            {"Artist Name": "Surely Shirley", "Song Title": "Track", "Source Directory": "unearthed", "Email": ""},
            {
                "Artist Name": "Surely Shirley",
                "Song Title": "Track",
                "Source Directory": "unearthed",
                "Email": "",
                "Facebook_URL": "https://facebook.com/surely-shirley",
            },
        ]
    )
    deduped = dedupe_pre_auto_validate(df, "Source Directory", night_mode=True)
    assert len(deduped.index) == 1
    assert deduped.iloc[0]["Facebook_URL"] == "https://facebook.com/surely-shirley"


def test_extract_fb_urls_from_generic_links() -> None:
    row = {
        "Social Link": "https://instagram.com/foo | https://www.facebook.com/panicboomband",
        "External Links": "https://fb.com/panicboom; https://example.com",
    }

    urls = night_mode_fb._extract_fb_urls_for_night_mode(row)

    assert urls == ["https://www.facebook.com/panicboomband", "https://fb.com/panicboom"]


def test_pass_a_uses_fb_url_from_social_link(monkeypatch) -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    monkeypatch.setattr(enricher, "_ensure_session", lambda: None)

    calls = {"urls": []}

    def _fake_scrape(url, *args, **kwargs):  # noqa: ANN001
        calls["urls"].append(url)
        night_result = night_mode_fb.NightModeFacebookResult(
            email="artist@test.com",
            email_all="artist@test.com",
            facebook_url=night_mode_fb._normalise_fb_url(url),
            email_extract_method="regex",
        )
        return night_result, ["artist@test.com"], "session", "found_email"

    monkeypatch.setattr(enricher, "_scrape_single_fb_candidate", _fake_scrape)

    row = {"Artist Name": "Test Artist", "Social Link": "https://www.facebook.com/panicboomband"}

    result = enricher.enrich_row_with_facebook_night(row)

    assert calls["urls"] == ["https://www.facebook.com/panicboomband"]
    assert result.get("FB_Status") == "pass_a_found_email"
    assert result.get("FB_Reason") == "explicit_url"
