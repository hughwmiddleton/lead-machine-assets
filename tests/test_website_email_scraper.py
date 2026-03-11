import website_email_scraper as wes


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

