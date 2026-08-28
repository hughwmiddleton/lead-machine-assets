import importlib.util
from pathlib import Path

import requests

import bandcamp_profile_engine as bpe


ARTIST_HTML = """
<html>
  <head>
    <meta property="og:site_name" content="Artist A">
    <meta name="keywords" content="dream pop, indie rock">
  </head>
  <body>
    <div class="location">melbourne, australia</div>
    <div id="bio-container">
      Contact artist@example.com. FFO: Slowdive, Beach House.
      <a href="https://artist-a.example/?utm_source=bandcamp">Website</a>
      <a href="https://instagram.com/artist_a">Profile</a>
      <a href="https://linktr.ee/artist_a">Links</a>
    </div>
    <div class="tralbum-tags"><a>Dream Pop</a><a>Indie Rock</a></div>
    <li class="music-grid-item">
      <span class="title">New EP</span>
      <span class="release-date">released August 2, 2025</span>
    </li>
  </body>
</html>
"""


class _Response:
    def __init__(self, text, status_code=200, url="https://artist-a.bandcamp.com/"):
        self.text = text
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def test_normal_artist_page_parses_rich_profile_and_preserves_canonical_url():
    session = _Session(_Response(ARTIST_HTML))
    result = bpe.fetch_bandcamp_profile(
        "http://artist-a.bandcamp.com/album/anything",
        session=session,
    )

    assert result.status == bpe.PROFILE_ACCEPTED
    assert result.canonical_url == "https://artist-a.bandcamp.com/"
    assert result.profile["profile_url"] == result.canonical_url
    assert result.profile["artist_name"] == "Artist A"
    assert result.profile["location"] == "Melbourne, Australia"
    assert set(result.profile["genres"]) == {"dream pop", "indie rock"}
    assert result.profile["primary_genre"] in {"dream pop", "indie rock"}
    assert result.profile["website"] == "https://artist-a.example/"
    assert result.profile["socials"]["instagram"] == "https://instagram.com/artist_a"
    assert result.profile["socials"]["linktree"] == "https://linktr.ee/artist_a"
    assert result.profile["email"] == "artist@example.com"
    assert result.profile["emails"] == ["artist@example.com"]
    assert result.profile["latest_release_title"] == "New EP"
    assert result.profile["latest_release_date"] == "2025-08-02"
    assert result.profile["sounds_like"] == "Slowdive, Beach House"
    assert result.identity_evidence["page_artist"] == "Artist A"


def test_http_200_client_challenge_is_neutrally_unavailable():
    result = bpe.fetch_bandcamp_profile(
        "https://artist-a.bandcamp.com/",
        session=_Session(_Response("<title>Client Challenge</title>")),
    )
    assert result.status == bpe.PROFILE_CHALLENGE_UNAVAILABLE
    assert result.reason == "client_challenge_title"


def test_recognized_soft_block_is_neutrally_unavailable():
    result = bpe.fetch_bandcamp_profile(
        "https://artist-a.bandcamp.com/",
        session=_Session(_Response("<body>Verify you are human</body>")),
    )
    assert result.status == bpe.PROFILE_CHALLENGE_UNAVAILABLE
    assert result.reason == "recognized_soft_block"


def test_network_failure_is_error_without_unbounded_retry():
    session = _Session(error=requests.ConnectionError("offline"))
    result = bpe.fetch_bandcamp_profile("https://artist-a.bandcamp.com/", session=session)
    assert result.status == bpe.PROFILE_ERROR
    assert result.reason == "network_error:ConnectionError"
    assert len(session.calls) == 1


def test_legitimate_artist_page_is_not_misclassified_as_challenge():
    assert bpe.bandcamp_challenge_reason(ARTIST_HTML) == ""


def test_browser_fallback_is_bounded_and_challenge_remains_neutral():
    calls = []
    result = bpe.fetch_bandcamp_profile(
        "https://artist-a.bandcamp.com/",
        session=_Session(_Response("<title>Client Challenge</title>")),
        browser_fetcher=lambda url: calls.append(url) or "<body>Verify you are human</body>",
    )
    assert result.status == bpe.PROFILE_CHALLENGE_UNAVAILABLE
    assert result.browser_used is True
    assert calls == ["https://artist-a.bandcamp.com/"]


def test_usable_http_page_does_not_invoke_browser_callback():
    calls = []
    result = bpe.fetch_bandcamp_profile(
        "https://artist-a.bandcamp.com/",
        session=_Session(_Response(ARTIST_HTML)),
        browser_fetcher=lambda url: calls.append(url) or "",
    )
    assert result.status == bpe.PROFILE_ACCEPTED
    assert result.browser_used is False
    assert calls == []


def test_challenge_browser_success_uses_same_rich_parser():
    calls = []
    result = bpe.fetch_bandcamp_profile(
        "https://artist-a.bandcamp.com/",
        session=_Session(_Response("<title>Client Challenge</title>")),
        browser_fetcher=lambda url: calls.append(url) or ARTIST_HTML,
        browser_on_empty=False,
    )
    assert result.status == bpe.PROFILE_ACCEPTED
    assert result.browser_used is True
    assert calls == ["https://artist-a.bandcamp.com/"]
    assert result.profile["artist_name"] == "Artist A"
    assert result.profile["location"] == "Melbourne, Australia"
    assert result.profile["emails"] == ["artist@example.com"]


def test_browser_callback_failure_preserves_challenge_unavailable():
    def fail(_url):
        raise RuntimeError("driver unavailable")

    result = bpe.fetch_bandcamp_profile(
        "https://artist-a.bandcamp.com/",
        session=_Session(_Response("<title>Client Challenge</title>")),
        browser_fetcher=fail,
        browser_on_empty=False,
    )
    assert result.status == bpe.PROFILE_CHALLENGE_UNAVAILABLE
    assert result.reason == "client_challenge_title"
    assert result.browser_used is True


def test_browser_non_artist_html_after_challenge_remains_unavailable():
    result = bpe.fetch_bandcamp_profile(
        "https://artist-a.bandcamp.com/",
        session=_Session(_Response("<title>Client Challenge</title>")),
        browser_fetcher=lambda _url: "<html><body>Temporary service page</body></html>",
        browser_on_empty=False,
    )
    assert result.status == bpe.PROFILE_CHALLENGE_UNAVAILABLE
    assert result.reason == "browser_profile_unavailable"
    assert result.browser_used is True


def test_challenge_without_browser_callback_has_no_selenium_dependency():
    result = bpe.fetch_bandcamp_profile(
        "https://artist-a.bandcamp.com/",
        session=_Session(_Response("<title>Client Challenge</title>")),
        browser_fetcher=None,
        browser_on_empty=False,
    )
    assert result.status == bpe.PROFILE_CHALLENGE_UNAVAILABLE
    assert result.browser_used is False


def test_challenge_only_mode_does_not_browse_after_network_error():
    calls = []
    result = bpe.fetch_bandcamp_profile(
        "https://artist-a.bandcamp.com/",
        session=_Session(error=requests.ConnectionError("offline")),
        browser_fetcher=lambda url: calls.append(url) or ARTIST_HTML,
        browser_on_empty=False,
    )
    assert result.status == bpe.PROFILE_ERROR
    assert result.browser_used is False
    assert calls == []


def test_gui_parser_adapter_matches_shared_profile_fields():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_bandcamp_parity", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    shared = bpe.parse_bandcamp_profile_html("https://artist-a.bandcamp.com/", ARTIST_HTML)
    gui = module._bandcamp_parse_html("https://artist-a.bandcamp.com/", ARTIST_HTML)
    for field in (
        "artist_name", "profile_url", "location", "genres", "email", "emails",
        "socials", "website", "latest_release_title", "latest_release_date",
        "latest_release_precision", "sounds_like", "primary_genre",
    ):
        assert gui[field] == shared[field]


HYGIENE_HTML = """
<html>
  <head>
    <meta property="og:site_name" content="Hygiene Artist">
    <meta name="keywords" content="indie pop, synth pop">
  </head>
  <body>
    <div class="location">leeds, uk</div>
    <a href="https://f4.bcbits.com/img/a123_10.jpg">Artwork</a>
    <a href="https://static.example.test/assets/logo.svg">Asset</a>
    <a href="https://hygieneartist.example/shows?utm_source=bandcamp">Official</a>
    <a href="https://www.instagram.com/hygiene.artist/">Instagram</a>
    <a href="https://instagram.com/p/not-a-profile">Post</a>
    <a href="https://twitter.com/intent/tweet">Share</a>
    <a href="https://facebook.com/sharer/sharer.php">Share</a>
    <a href="https://www.tiktok.com/@hygieneartist">TikTok</a>
    <a href="https://open.spotify.com/track/not-an-artist">Track</a>
    <a href="https://discogs.com/artist/123">Database</a>
    <a href="https://www.songkick.com/concerts/123-hygiene-artist">Event</a>
    <a href="https://www.songkick.com/artists/123-hygiene-artist">Songkick artist</a>
    <a href="https://linktr.ee/hygieneartist">Hub</a>
    <div id="bio-container">
      Contact one@example.com and bookings@example.org.
      Instagram: Twitter. Twitter: Facebook. Instagram: discography.
      Twitter: Nov. Instagram: tiktok.com.
    </div>
    <div class="tralbum-tags"><a>Indie Pop</a><a>Synth Pop</a></div>
    <li class="music-grid-item">
      <span class="title">Clean Release</span>
      <span class="release-date">released June 4, 2026</span>
    </li>
  </body>
</html>
"""


def test_bandcamp_parser_filters_assets_and_prefers_explicit_social_profiles():
    profile = bpe.parse_bandcamp_profile_html(
        "https://hygieneartist.bandcamp.com/", HYGIENE_HTML
    )

    assert profile["website"] == "https://hygieneartist.example/shows"
    assert profile["socials"]["instagram"] == "https://www.instagram.com/hygiene.artist/"
    assert profile["socials"]["twitter"] == ""
    assert profile["socials"]["facebook"] == ""
    assert profile["socials"]["tiktok"] == "https://www.tiktok.com/@hygieneartist"
    assert profile["socials"]["songkick"] == "https://www.songkick.com/artists/123-hygiene-artist"
    assert profile["socials"]["linktree"] == "https://linktr.ee/hygieneartist"
    assert all("bcbits.com" not in value for value in profile["all_social_links"])
    assert all("/assets/" not in value for value in profile["all_social_links"])
    assert all("/concerts/" not in value for value in profile["all_social_links"])
    assert all("open.spotify.com/track/" not in value for value in profile["all_social_links"])
    assert all("discogs.com" not in value for value in profile["all_social_links"])


def test_bandcamp_hygiene_preserves_email_and_profile_metadata_parity():
    profile = bpe.parse_bandcamp_profile_html(
        "https://hygieneartist.bandcamp.com/", HYGIENE_HTML
    )

    assert profile["artist_name"] == "Hygiene Artist"
    assert profile["location"] == "Leeds, Uk"
    assert set(profile["genres"]) == {"indie pop", "synth pop"}
    assert profile["latest_release_title"] == "Clean Release"
    assert profile["latest_release_date"] == "2026-06-04"
    assert profile["email"] == "one@example.com"
    assert profile["emails"] == ["one@example.com", "bookings@example.org"]


def test_no_credible_artist_site_leaves_website_empty():
    html = """
    <meta property="og:site_name" content="No Website Artist">
    <a href="https://f4.bcbits.com/img/a123_10.jpg">Artwork</a>
    <a href="https://open.spotify.com/album/123">Streaming</a>
    <a href="https://discogs.com/artist/123">Database</a>
    <a href="https://www.songkick.com/concerts/123-artist">Event</a>
    """
    profile = bpe.parse_bandcamp_profile_html(
        "https://nowebsite.bandcamp.com/", html
    )
    assert profile["website"] == ""
    assert profile["all_social_links"] == []


def test_legitimate_inferred_handle_is_kept_when_no_explicit_profile_exists():
    html = """
    <meta property="og:site_name" content="Handle Artist">
    <div id="bio-container">Instagram: @legit.artist</div>
    """
    profile = bpe.parse_bandcamp_profile_html("https://handles.bandcamp.com/", html)
    assert profile["socials"]["instagram"] == "https://instagram.com/legit.artist"


def test_explicit_profile_is_not_overwritten_by_inferred_handle():
    html = """
    <meta property="og:site_name" content="Priority Artist">
    <a href="https://instagram.com/priority.artist">Instagram</a>
    <div id="bio-container">Instagram: @wrong.artist</div>
    """
    profile = bpe.parse_bandcamp_profile_html("https://priority.bandcamp.com/", html)
    assert profile["socials"]["instagram"] == "https://instagram.com/priority.artist"


def test_generic_and_domain_like_inferred_handles_are_rejected():
    html = """
    <meta property="og:site_name" content="Rejected Handles">
    <div id="bio-container">
      Instagram: Twitter. Twitter: Facebook. Instagram: discography.
      Twitter: December. Instagram: tiktok.com.
    </div>
    """
    profile = bpe.parse_bandcamp_profile_html("https://rejected.bandcamp.com/", html)
    assert profile["socials"]["instagram"] == ""
    assert profile["socials"]["twitter"] == ""
