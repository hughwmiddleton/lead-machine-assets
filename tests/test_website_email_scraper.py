import json

import website_email_scraper as wes
from email_provenance import EMAIL_PROVENANCE_JSON_COL


def test_collect_candidate_websites_skips_schemeless_spotify_platform_url():
    row = {
        "Website": "open.spotify.com/artist/artist-a",
        "Spotify_URL": "",
        "Spotify_Website_URL": "",
        "External Links": "",
        "Social Link": "",
    }

    assert wes._collect_candidate_websites(row) == []


def test_collect_candidate_websites_skips_instagram_platform_url():
    row = {
        "Website": "instagram.com/artist-a",
        "Spotify_URL": "",
        "Spotify_Website_URL": "",
        "External Links": "",
        "Social Link": "",
    }

    assert wes._collect_candidate_websites(row) == []


def test_collect_candidate_websites_allows_real_domain():
    row = {
        "Website": "artistname.com",
        "Spotify_URL": "",
        "Spotify_Website_URL": "",
        "External Links": "",
        "Social Link": "",
    }

    assert wes._collect_candidate_websites(row) == ["artistname.com"]


def test_spotify_url_fallback_does_not_produce_crawl_target(monkeypatch):
    rows = [
        {
            "Artist Name": "Artist A",
            "Spotify_URL": "https://open.spotify.com/artist/artist-a",
            "Spotify_Website_URL": "",
            "Website": "",
            "External Links": "",
            "Social Link": "",
            "Email": "",
        }
    ]

    def fail_fetch(*args, **kwargs):
        raise AssertionError("legacy website-email fetch should not run for Spotify platform URLs")

    monkeypatch.setattr(wes, "_fetch_html", fail_fetch)

    enriched = wes.enrich_rows_with_website_emails(rows)

    assert enriched[0]["Email"] == ""
    assert enriched[0]["Email_All"] == ""


def test_website_scraper_writes_canonical_email_with_website_enrich_provenance(monkeypatch):
    rows = [
        {
            "Artist Name": "Web Artist",
            "Spotify_Website_URL": "https://artist.test",
            "Website": "",
            "External Links": "",
            "Social Link": "",
            "Email": "",
            "Email_All": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    ]

    def fake_fetch(session, url):
        return "<html><body>booking@artist.test info@artist.test</body></html>"

    monkeypatch.setattr(wes, "_fetch_html", fake_fetch)

    enriched = wes.enrich_rows_with_website_emails(rows)

    assert enriched[0]["Email"] == "booking@artist.test"
    assert enriched[0]["Email_All"] == "booking@artist.test;info@artist.test"
    assert enriched[0]["Email_Source_Type"] == "website_enrich"
    assert enriched[0]["Email_Source_URL"] == "https://artist.test"
    assert enriched[0]["Email_Extract_Method"] == "regex"
    assert enriched[0]["Email Source"] == "Website Enrich"
    assert enriched[0]["Seed_Directory_Email"] == "booking@artist.test"
    assert enriched[0]["Seed_Directory_Email_All"] == "booking@artist.test;info@artist.test"

    provenance = json.loads(enriched[0][EMAIL_PROVENANCE_JSON_COL])
    assert provenance["booking@artist.test"]["source_type"] == "website_enrich"
    assert provenance["booking@artist.test"]["source_url"] == "https://artist.test"


def test_website_scraper_preserves_existing_email(monkeypatch):
    rows = [
        {
            "Artist Name": "Existing Artist",
            "Spotify_Website_URL": "https://artist.test",
            "Email": "existing@label.test",
            "Email_All": "existing@label.test",
            "Email_Source_Type": "soundcloud_live",
        }
    ]

    def fail_fetch(*args, **kwargs):
        raise AssertionError("fetch should not run when email already exists")

    monkeypatch.setattr(wes, "_fetch_html", fail_fetch)

    enriched = wes.enrich_rows_with_website_emails(rows)

    assert enriched[0]["Email"] == "existing@label.test"
    assert enriched[0]["Email_All"] == "existing@label.test"


def test_website_scraper_filters_wix_sentry_telemetry(monkeypatch):
    rows = [
        {
            "Artist Name": "Telemetry Artist",
            "Spotify_Website_URL": "https://artist.test",
            "Email": "",
            "Email_All": "",
        }
    ]

    def fake_fetch(session, url):
        return (
            "<html><body>"
            "abc@sentry.wixpress.com "
            "def@sentry-next.wixpress.com "
            "booking@artist.test"
            "</body></html>"
        )

    monkeypatch.setattr(wes, "_fetch_html", fake_fetch)

    enriched = wes.enrich_rows_with_website_emails(rows)

    assert enriched[0]["Email"] == "booking@artist.test"
    assert "booking@artist.test" in enriched[0]["Email_All"]
    assert "sentry.wixpress.com" not in enriched[0]["Email_All"]
    assert "sentry-next.wixpress.com" not in enriched[0]["Email_All"]


def test_website_scraper_rejects_reverbnation_promotion_false_positive():
    html = (
        "<a href='https://www.reverbnation.com/band-promotion/'>"
        "Learn how to place music on itunes at ReverbNation.com"
        "</a>"
    )

    assert wes._extract_emails_from_html(html) == []


def test_website_scraper_rejects_image_filename_false_positive():
    html = "<img src='screenshot-2025-10-09-at-5-30-08-pm.png'>"

    assert wes._extract_emails_from_html(html) == []


def test_website_scraper_rejects_placeholder_but_keeps_real_contact():
    html = "user@domain.com booking@artist.test"

    assert wes._extract_emails_from_html(html) == ["booking@artist.test"]


def test_website_scraper_generic_contact_page_sets_needs_review(monkeypatch):
    rows = [
        {
            "Artist Name": "Generic Artist",
            "Spotify_Website_URL": "https://artist.test",
            "Email": "",
            "Email_All": "",
        }
    ]

    def fake_fetch(session, url):
        return "<html><body>contact@artist.test</body></html>" if "/contact" in url else "<html><body><a href='/contact'>Contact</a></body></html>"

    monkeypatch.setattr(wes, "_fetch_html", fake_fetch)

    enriched = wes.enrich_rows_with_website_emails(rows)

    assert enriched[0]["Email"] == "contact@artist.test"
    assert enriched[0]["Email Source"] == "Website Enrich (generic contact page)"
    assert enriched[0]["Needs_Review"] == "TRUE"
