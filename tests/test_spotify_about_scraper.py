import requests

import spotify_about_scraper as sas


class _DummySession(requests.Session):
    pass


def test_fetch_artist_html_returns_empty_on_non_200_requests(monkeypatch):
    # Simulate requests path returning non-200; should yield empty string.
    monkeypatch.setattr(
        sas,
        "fetch_html",
        lambda url, **kwargs: {"mode_used": "requests", "status": 500, "html": "<html>blocked</html>"},
    )
    session = _DummySession()
    html = sas._fetch_artist_html(session, "https://spotify.com/artist/123", logger=None)
    assert html == ""
