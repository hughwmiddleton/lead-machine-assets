import json

import pandas as pd
import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde
import night_mode_fb as nmfb
import pipeline_runner
from email_provenance import EMAIL_PROVENANCE_JSON_COL, _set_email_with_provenance


def _make_worker(logs):
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    worker.progress = SimpleNamespace(emit=lambda *args, **kwargs: None)
    worker._set_platform_state = lambda *args, **kwargs: None
    worker._platform_attempt_allowed = lambda *args, **kwargs: True
    worker._fb_session_auth_checked = True
    worker._fb_session_authenticated = True
    worker._fb_session_auth_reason = "authenticated"
    worker._fb_session_invalid = False
    worker._fb_discovery_disabled = False
    worker._fb_discovery_disabled_reason = ""
    return worker


def _seed_df(row):
    df = pd.DataFrame([row], dtype=str).fillna("")
    return df


class _FakeBodyElement:
    def __init__(self, text: str):
        self.text = text

    def get_attribute(self, name: str):
        if name == "innerText":
            return self.text
        return ""


class _RenderedTextDriver:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.current_url = ""
        self.page_source = ""
        self._rendered_text = ""

    def get(self, url):  # noqa: ANN001
        self.calls.append(url)
        self.current_url = url
        page = self.pages.get(url, {})
        self.page_source = page.get("html", "")
        self._rendered_text = page.get("rendered_text", "")

    def find_element(self, _by, value):  # noqa: ANN001
        if value == "body":
            return _FakeBodyElement(self._rendered_text)
        raise LookupError(value)

    def execute_script(self, script, *args):  # noqa: ANN001
        page = self.pages.get(self.current_url, {})
        script_text = str(script or "")
        if "document.body" in script_text:
            return self._rendered_text
        if "fb_visible_text_container_blocks" in script_text:
            return list(page.get("fb_container_texts", []))
        if "fb_visible_text_region_fragment_fallback" in script_text:
            return list(page.get("fb_region_fragments", []))
        return []


def test_discover_facebook_url_bounded_requires_strong_candidate(monkeypatch):
    calls = []

    class _DummyClient:
        pass

    monkeypatch.setattr(cde, "FacebookSearchClient", lambda driver, logger: _DummyClient())

    def fake_find_best_page(
        artist_name,
        extra_signal,
        fb_client,
        logger,
        require_strong_candidate=False,
        defer_identity_floor_to_postscrape=False,
        skip_login_check=False,
    ):
        calls.append(
            {
                "artist_name": artist_name,
                "extra_signal": extra_signal,
                "require_strong_candidate": require_strong_candidate,
                "defer_identity_floor_to_postscrape": defer_identity_floor_to_postscrape,
                "skip_login_check": skip_login_check,
            }
        )
        return "https://www.facebook.com/strongband"

    monkeypatch.setattr(cde, "facebook_find_best_page", fake_find_best_page)

    result = cde._discover_facebook_url_bounded(object(), "The Midnight Echo", "", logger=None)

    assert result == "https://www.facebook.com/strongband"
    assert calls == [
        {
            "artist_name": "The Midnight Echo",
            "extra_signal": "",
            "require_strong_candidate": True,
            "defer_identity_floor_to_postscrape": False,
            "skip_login_check": True,
        }
    ]


@pytest.mark.parametrize(
    ("row_overrides", "expected_extra_signal"),
    [
        ({"Location": "", "Song Title": ""}, ""),
        ({"SoundCloud Link": "https://soundcloud.com/signalhandle/sets/live", "Location": "", "Song Title": ""}, "signalhandle"),
        ({"SoundCloud Link": "", "Bandcamp_URL": "https://night-light.bandcamp.com/album/demo", "Location": "", "Song Title": ""}, "night-light"),
        ({"SoundCloud Link": "", "Bandcamp_URL": "", "Source URL": "https://soundcloud.com/sourcehandle/night-drive", "Location": "", "Song Title": ""}, "sourcehandle"),
        ({"SoundCloud Link": "https://soundcloud.com/discover", "Bandcamp_URL": "https://bandcamp.com/discover/electronic", "Source URL": "https://example.com/artist", "Location": "", "Song Title": ""}, ""),
        ({"SoundCloud Link": "https://soundcloud.com/charts/top", "Bandcamp_URL": "https://blog.bandcamp.com/article", "Location": "", "Song Title": ""}, ""),
        ({"SoundCloud Link": "https://soundcloud.com/search?q=signal", "Bandcamp_URL": "https://help.bandcamp.com/hc/en-us", "Location": "", "Song Title": ""}, ""),
        ({"Location": "Melbourne", "Song Title": "Night Drive"}, "Night Drive"),
        ({"Location": "", "Song Title": "Night Drive"}, "Night Drive"),
        ({"Location": "", "Song Title": "Headside In Da Skiez (Babycham Supernova)"}, "Headside In Da Skiez"),
        ({"Location": "", "Song Title": "TAKE//OVER"}, "TAKE OVER"),
        ({"Location": "Melbourne, VIC", "Song Title": "Demo Mix"}, "Melbourne VIC"),
        ({"Location": "", "Song Title": "   Song   Title   "}, ""),
        ({"Location": "", "Song Title": "(Live)"}, ""),
        ({"Location": "", "Song Title": "Song feat. Artist"}, "Song feat. Artist"),
        ({"Location": "Melbourne", "Song Title": "TAKE//OVER"}, "TAKE OVER"),
    ],
)
def test_discover_facebook_identity_selects_single_extra_signal(monkeypatch, row_overrides, expected_extra_signal):
    logs = []
    worker = _make_worker(logs)
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    worker._ensure_fb_discovery_session = lambda driver: (True, "authenticated")
    driver = object()
    seed_df = _seed_df(
        {
            "Artist Name": "Signal Artist",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Social Link": "",
            "External Links": "",
            "FB_Status": "",
            "FB_Reason": "",
            "Location": "",
            "Song Title": "",
            "SoundCloud Link": "",
            "Bandcamp_URL": "",
            "Source URL": "",
        }
    )
    for key, value in row_overrides.items():
        seed_df.at[0, key] = value
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, extra_signal, logger):
        discover_calls.append((fb_driver, artist_name, extra_signal))
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    matched = worker._discover_facebook_identity(seed_df, 0, driver, ctx)

    assert matched is False
    assert discover_calls == [(driver, "Signal Artist", expected_extra_signal)]


def test_facebook_candidate_is_strong_accepts_music_category():
    candidate = cde.FbCandidate(
        name="The Midnight Echo",
        url="https://www.facebook.com/themidnightecho",
        category="Musician/Band",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "The Midnight Echo",
        candidate,
        "<html><body><div>Musician/Band</div></body></html>",
        "Musician/Band",
        ["Musician/Band"],
        [],
    )

    assert accepted is True
    assert reason == "music_category"


def test_facebook_candidate_is_strong_rejects_personal_profile_phrase():
    candidate = cde.FbCandidate(
        name="John Smith",
        url="https://www.facebook.com/johnsmith",
        category="",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "John Smith",
        candidate,
        "<html><body><div>John Smith is on Facebook</div></body></html>",
        "",
        ["John Smith is on Facebook"],
        [],
    )

    assert accepted is False
    assert reason == "personal_profile_phrase"


def test_facebook_candidate_is_strong_rejects_slug_only_weak_match():
    candidate = cde.FbCandidate(
        name="Skyline",
        url="https://www.facebook.com/skyline",
        category="",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "Skyline",
        candidate,
        "<html><body><div>Welcome to Skyline</div></body></html>",
        "",
        ["Welcome to Skyline"],
        [],
    )

    assert accepted is False
    assert reason == "slug_or_name_only_match"


def test_facebook_candidate_is_strong_rejects_short_name_without_positive_signals():
    candidate = cde.FbCandidate(
        name="Sun",
        url="https://www.facebook.com/sun",
        category="",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "Sun",
        candidate,
        "<html><body><div>Sun</div></body></html>",
        "",
        ["Sun"],
        [],
    )

    assert accepted is False
    assert reason == "short_name_without_strong_signal"


def test_facebook_candidate_is_strong_accepts_short_name_with_music_platform_link():
    candidate = cde.FbCandidate(
        name="Sun",
        url="https://www.facebook.com/sun",
        category="",
    )

    accepted, reason = cde._facebook_candidate_is_strong(
        "Sun",
        candidate,
        '<html><body><a href="https://soundcloud.com/sun">SoundCloud</a></body></html>',
        "",
        ["Listen now"],
        ["https://soundcloud.com/sun"],
    )

    assert accepted is True
    assert reason == "music_platform_link"


@pytest.mark.parametrize(
    "row_overrides,expected_call",
    [
        (
            {"facebook_url": "https://www.facebook.com/socialfb", "Social Link": ""},
            "https://www.facebook.com/socialfb",
        ),
        (
            {"facebook_url": "", "Social Link": "facebook.com/socialfb"},
            "https://www.facebook.com/socialfb",
        ),
    ],
)
def test_fb_enrich_runs_when_fb_url_present_or_promotable(monkeypatch, row_overrides, expected_call):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Has FB URL",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Social Link": "",
            "External Links": "",
            "Source Directory": "",
            "Source URL": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    for key, val in row_overrides.items():
        seed_df.at[0, key] = val
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    calls = []

    def fake_extract(driver, url, log_fn=None, **kwargs):
        calls.append(url)
        return (["fb@example.com"], "https://www.facebook.com/socialfb/about", "")

    def fail_discover(*args, **kwargs):
        raise AssertionError("discovery should not run when a canonical/promotable facebook_url already exists")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fail_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert calls == [expected_call]
    assert seed_df.at[0, "Email"] == "fb@example.com"
    assert "fb@example.com" in seed_df.at[0, "Email_All"]
    assert seed_df.at[0, "FB_Status"].lower() == "found_email"
    assert not any("already has facebook link" in msg.lower() for msg in logs)


def test_fb_enrich_same_email_preserves_native_undiscovered_provenance(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    source_url = "https://undiscovered.music/artists/native-artist"
    native_provenance = {
        "artist@example.com": {
            "source_type": "undiscovered_music_profile",
            "surface": "undiscovered_music_profile",
            "source_url": source_url,
            "extract_method": "profile_direct",
        }
    }
    seed_df = _seed_df(
        {
            "Artist Name": "Native Artist",
            "Lead_Source": "Undiscovered Music",
            "Source_Directory": "undiscovered_music",
            "Source Directory": "Undiscovered Music",
            "Source URL": source_url,
            "Email": "artist@example.com",
            "Email_All": "artist@example.com",
            "Email_Type": "source_native",
            "Email_Source_URL": source_url,
            "Email_Source_Type": "undiscovered_music_profile",
            "Email_Extract_Method": "profile_direct",
            EMAIL_PROVENANCE_JSON_COL: json.dumps(native_provenance),
            "facebook_url": "https://www.facebook.com/nativeartist",
            "Facebook_URL": "https://www.facebook.com/nativeartist",
            "Social Link": "https://www.facebook.com/nativeartist",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_extract_fb_emails_bounded",
        lambda *args, **kwargs: (
            ["artist@example.com"],
            "https://www.facebook.com/nativeartist/about",
            "",
        ),
    )

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "artist@example.com"
    assert seed_df.at[0, "Email_All"] == "artist@example.com"
    assert seed_df.at[0, "Email_Type"] == "source_native"
    assert seed_df.at[0, "Email_Source_URL"] == source_url
    assert seed_df.at[0, "Email_Source_Type"] == "undiscovered_music_profile"
    assert seed_df.at[0, "Email_Extract_Method"] == "profile_direct"
    assert json.loads(seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]) == native_provenance
    assert not str(seed_df.loc[0].get("__fb_emails_applied", "") or "").strip()
    assert seed_df.at[0, "Lead_Source"] == "Undiscovered Music"
    assert seed_df.at[0, "Source_Directory"] == "undiscovered_music"
    assert seed_df.at[0, "Source URL"] == source_url


def test_fb_enrich_skips_same_fb_url_that_already_produced_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Same FB URL",
            "Email": "fb@example.com",
            "Email_All": "fb@example.com",
            "facebook_url": "https://www.facebook.com/samefb",
            "Facebook_URL": "https://www.facebook.com/samefb",
            "Social Link": "",
            "External Links": "",
            "Source Directory": "",
            "Source URL": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    _set_email_with_provenance(
        (seed_df, 0),
        "fb@example.com",
        source_url="https://www.facebook.com/samefb",
        source_type="facebook_enrich",
        method="regex",
        surface="facebook_main",
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    def fake_extract(driver, url, log_fn=None, **kwargs):
        calls.append(url)
        return (["new@example.com"], url, "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert calls == []
    assert any("Skipping previously successful Facebook URL" in msg for msg in logs)


def test_fb_enrich_existing_ig_email_does_not_block_fb(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "IG Email FB Allowed",
            "Email": "ig@example.com",
            "Email_All": "ig@example.com",
            "facebook_url": "https://www.facebook.com/fballowed",
            "Facebook_URL": "https://www.facebook.com/fballowed",
            "Social Link": "",
            "External Links": "",
            "Source Directory": "",
            "Source URL": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    _set_email_with_provenance(
        (seed_df, 0),
        "ig@example.com",
        source_url="https://www.instagram.com/fballowed/",
        source_type="instagram_enrich",
        method="regex",
        surface="instagram_profile",
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    def fake_extract(driver, url, log_fn=None, **kwargs):
        calls.append(url)
        return (["fb@example.com"], "https://www.facebook.com/fballowed", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert calls == ["https://www.facebook.com/fballowed"]
    assert set(seed_df.at[0, "Email_All"].split(";")) == {"ig@example.com", "fb@example.com"}


def test_fb_enrich_placeholder_same_url_does_not_create_success_skip(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Placeholder FB",
            "Email": "name@example.com",
            "Email_All": "name@example.com",
            "facebook_url": "https://www.facebook.com/placeholderfb",
            "Facebook_URL": "https://www.facebook.com/placeholderfb",
            "Social Link": "",
            "External Links": "",
            "Source Directory": "",
            "Source URL": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    _set_email_with_provenance(
        (seed_df, 0),
        "name@example.com",
        source_url="https://www.facebook.com/placeholderfb",
        source_type="facebook_enrich",
        method="regex",
        surface="facebook_main",
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    def fake_extract(driver, url, log_fn=None, **kwargs):
        calls.append(url)
        return (["real@example.com"], "https://www.facebook.com/placeholderfb", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert calls == ["https://www.facebook.com/placeholderfb"]


def test_fb_enrich_uses_explicit_share_entrypoint_without_discovery(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    worker._get_night_fb_share_promotion_resolver = lambda: (
        lambda raw: "https://www.facebook.com/artistsharepage"
    )
    seed_df = _seed_df(
        {
            "Artist Name": "Share Entrypoint",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr",
            "External Links": "",
            "Source Directory": "",
            "Source URL": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    calls = []

    def fake_extract(driver, url, log_fn=None, **kwargs):
        calls.append(url)
        return (["sharefb@example.com"], "https://www.facebook.com/artistsharepage", "")

    def fail_discover(*args, **kwargs):
        raise AssertionError("discovery should not run when an explicit share entrypoint is present")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fail_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert calls == ["https://www.facebook.com/artistsharepage"]
    assert seed_df.at[0, "Email"] == "sharefb@example.com"
    assert seed_df.at[0, "Facebook_URL"] == "https://www.facebook.com/artistsharepage"
    assert seed_df.at[0, "facebook_url"] == "https://www.facebook.com/artistsharepage"


def test_fb_enrich_uses_payload_promoted_facebook_url_without_discovery(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Payload Promoted FB",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "",
            "External Links": "",
            "Source Directory": "",
            "Source URL": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    payload = cde.EnrichmentPayload(
        socials={"https://fb.com/payloadpromotedfb"},
        websites=set(),
        source_dir="soundcloud",
        source_url="https://soundcloud.com/payloadpromotedfb",
        match_score=0.95,
    )

    assert worker._apply_payload_guarded(seed_df, 0, payload, "Payload Promoted FB") is True
    assert seed_df.at[0, "Facebook_URL"] == "https://www.facebook.com/payloadpromotedfb"

    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    calls = []

    def fake_extract(driver, url, log_fn=None, **kwargs):
        calls.append(url)
        return (["payloadfb@example.com"], "https://www.facebook.com/payloadpromotedfb/about", "")

    def fail_discover(*args, **kwargs):
        raise AssertionError("discovery should not run when accepted payload promotion already populated Facebook_URL")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fail_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert calls == ["https://www.facebook.com/payloadpromotedfb"]
    assert seed_df.at[0, "Email"] == "payloadfb@example.com"
    assert "payloadfb@example.com" in seed_df.at[0, "Email_All"]
    assert seed_df.at[0, "FB_Status"].lower() == "found_email"


def test_fb_enrich_uses_rendered_visible_text_when_page_source_has_no_email() -> None:
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered FB",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/renderedfb",
            "Facebook_URL": "https://www.facebook.com/renderedfb",
            "Facebook URL": "https://www.facebook.com/renderedfb",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    driver = _RenderedTextDriver(
        {
            "https://www.facebook.com/renderedfb": {
                "html": '<html><body><a href="/renderedfb/about">About</a></body></html>',
                "rendered_text": "No email on main page",
            },
            "https://www.facebook.com/renderedfb/about": {
                "html": "<html><body><div>About Contact and basic info Contact info</div></body></html>",
                "rendered_text": "About Contact and basic info Contact info divebaryouth.artist@gmail.com",
            },
        }
    )

    matched = worker._enrich_row_facebook(seed_df, 0, driver, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "divebaryouth.artist@gmail.com"
    assert "divebaryouth.artist@gmail.com" in seed_df.at[0, "Email_All"]
    assert seed_df.at[0, "FB_Status"] == "found_email"
    assert driver.calls[-2:] == [
        "https://www.facebook.com/renderedfb",
        "https://www.facebook.com/renderedfb/about",
    ]


def test_fb_enrich_uses_main_page_rendered_visible_text_without_secondary_fetch() -> None:
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered FB Main",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/renderedmainfb",
            "Facebook_URL": "https://www.facebook.com/renderedmainfb",
            "Facebook URL": "https://www.facebook.com/renderedmainfb",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    driver = _RenderedTextDriver(
        {
            "https://www.facebook.com/renderedmainfb": {
                "html": '<html><body><a href="/renderedmainfb/about">About</a></body></html>',
                "rendered_text": "Intro Contact brighteyedbookings@gmail.com",
            },
            "https://www.facebook.com/renderedmainfb/about": {
                "html": "<html><body><div>No email here</div></body></html>",
                "rendered_text": "No email here",
            },
        }
    )

    matched = worker._enrich_row_facebook(seed_df, 0, driver, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "brighteyedbookings@gmail.com"
    assert "brighteyedbookings@gmail.com" in seed_df.at[0, "Email_All"]
    assert seed_df.at[0, "FB_Status"] == "found_email"
    assert driver.calls[-1] == "https://www.facebook.com/renderedmainfb"
    assert "https://www.facebook.com/renderedmainfb/about" not in driver.calls


def test_fb_enrich_uses_fb_container_fallback_without_secondary_fetch() -> None:
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered FB Container",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/renderedcontainerfb",
            "Facebook_URL": "https://www.facebook.com/renderedcontainerfb",
            "Facebook URL": "https://www.facebook.com/renderedcontainerfb",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    driver = _RenderedTextDriver(
        {
            "https://www.facebook.com/renderedcontainerfb": {
                "html": '<html><body><a href="/renderedcontainerfb/about">About</a></body></html>',
                "rendered_text": "No email on main page",
                "fb_container_texts": ["Intro Contact brighteyedbookings@gmail.com"],
            },
            "https://www.facebook.com/renderedcontainerfb/about": {
                "html": "<html><body><div>No email here</div></body></html>",
                "rendered_text": "No email here",
                "fb_container_texts": [],
            },
        }
    )

    matched = worker._enrich_row_facebook(seed_df, 0, driver, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "brighteyedbookings@gmail.com"
    assert "brighteyedbookings@gmail.com" in seed_df.at[0, "Email_All"]
    assert seed_df.at[0, "FB_Status"] == "found_email"
    assert driver.calls[-1] == "https://www.facebook.com/renderedcontainerfb"
    assert "https://www.facebook.com/renderedcontainerfb/about" not in driver.calls


def test_fb_enrich_rendered_visible_text_without_email_keeps_no_email_status() -> None:
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered No Email",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/renderednoemail",
            "Facebook_URL": "https://www.facebook.com/renderednoemail",
            "Facebook URL": "https://www.facebook.com/renderednoemail",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    driver = _RenderedTextDriver(
        {
            "https://www.facebook.com/renderednoemail": {
                "html": '<html><body><a href="/renderednoemail/about">About</a></body></html>',
                "rendered_text": "No email on main page",
            },
            "https://www.facebook.com/renderednoemail/about": {
                "html": "<html><body><div>About Contact and basic info Contact info</div></body></html>",
                "rendered_text": "About Contact and basic info Contact info",
            },
        }
    )

    matched = worker._enrich_row_facebook(seed_df, 0, driver, ctx)

    assert matched is False
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert seed_df.at[0, "FB_Status"] == "no_email_on_page"
    assert driver.calls[-2:] == [
        "https://www.facebook.com/renderednoemail",
        "https://www.facebook.com/renderednoemail/about",
    ]


def test_fb_enrich_handles_missing_email_columns(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Missing Email Columns",
                "Social Link": "facebook.com/missingcolumns",
                "External Links": "facebook.com/missingcolumns",
            }
        ],
        dtype=str,
    ).fillna("")
    seed_df = cde._ensure_email_columns(seed_df)
    seed_df = cde._apply_fb_promotion_df(seed_df)
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    calls = []

    def fake_extract(driver, url, log_fn=None, **kwargs):
        calls.append(url)
        return ([], url, "no_email_on_page")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert calls, "FB extraction should be attempted when a promotable link exists"
    assert "Email_All" in seed_df.columns
    assert seed_df.at[0, "Email_All"] == ""


def test_fb_gate_skips_weak_row_without_terminal_status(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "DJ",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_discover_facebook_url_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("facebook discovery should be gated")),
    )
    monkeypatch.setattr(
        cde,
        "_extract_fb_emails_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("facebook extraction should be gated")),
    )

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert "FB_Status" not in seed_df.columns
    assert any("skipping fb heavy enrichment" in msg.lower() for msg in logs)


def test_fb_explicit_email_discovery_increments_summary(monkeypatch):
    pipeline_runner.reset_email_summary_counts()
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Has FB URL",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/socialfb",
            "Facebook_URL": "https://www.facebook.com/socialfb",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_extract(driver, url, log_fn=None, **kwargs):
        return (["fb@example.com"], "https://www.facebook.com/socialfb/about", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "fb@example.com"
    assert pipeline_runner.get_email_summary_counts()["emails_found"] == 1


def test_fb_enrich_success_normalizes_stale_pass_a_status_fields(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._row_is_spotify_origin = lambda *args, **kwargs: True
    seed_df = _seed_df(
        {
            "Artist Name": "godlands",
            "Email": "alex@fatcatmusicgroup.com",
            "Email_All": "alex@fatcatmusicgroup.com",
            "facebook_url": "https://www.facebook.com/iamgodlands",
            "Facebook_URL": "https://www.facebook.com/iamgodlands",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "https://www.instagram.com/iamgodlands/",
            "Email_Source_Type": "instagram_enrich",
            "Email_Extract_Method": "regex",
            "Email_Provenance_JSON": json.dumps(
                {
                    "alex@fatcatmusicgroup.com": {
                        "extract_method": "regex",
                        "source_type": "instagram_enrich",
                        "source_url": "https://www.instagram.com/iamgodlands/",
                        "surface": "instagram_profile",
                    }
                }
            ),
            "FB_Status": "pass_a_no_email_on_page",
            "FB_Attempt_State": "attempted_fb_no_email_on_page",
            "FB_Write_State": "fb_no_email_written",
            "FB_Debug_Reason": "no_email_visible",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_extract(driver, url, log_fn=None, **kwargs):
        return (["mikeadams@littleempiremusic.com"], url, "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "alex@fatcatmusicgroup.com"
    assert pipeline_runner.normalize_emails(seed_df.at[0, "Email_All"]) == [
        "alex@fatcatmusicgroup.com",
        "mikeadams@littleempiremusic.com",
    ]
    assert seed_df.at[0, "__fb_emails_applied"] == "mikeadams@littleempiremusic.com"
    assert seed_df.at[0, "FB_Status"] == "pass_a_found_email"
    assert seed_df.at[0, "FB_Attempt_State"] == "attempted_fb_found_email"
    assert seed_df.at[0, "FB_Write_State"] == "fb_wrote_email_all_only"
    assert seed_df.at[0, "FB_Debug_Reason"] == "email_written"
    provenance = json.loads(seed_df.at[0, "Email_Provenance_JSON"])
    assert provenance["mikeadams@littleempiremusic.com"]["source_type"] == "facebook_enrich"
    assert provenance["mikeadams@littleempiremusic.com"]["source_url"] == "https://www.facebook.com/iamgodlands"


def test_fb_enrich_skips_when_same_fb_url_already_succeeded(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Has Email",
            "Email": "existing@example.com",
            "Email_All": "existing@example.com",
            "facebook_url": "https://www.facebook.com/socialfb",
            "Facebook_URL": "https://www.facebook.com/socialfb",
            "Social Link": "",
            "External Links": "",
        }
    )
    _set_email_with_provenance(
        (seed_df, 0),
        "existing@example.com",
        source_url="https://www.facebook.com/socialfb",
        source_type="facebook_enrich",
        method="regex",
        surface="facebook_main",
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    called = False

    def fake_extract(*args, **kwargs):
        nonlocal called
        called = True
        return (["should-not-run"], "https://www.facebook.com/socialfb", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert called is False
    assert seed_df.at[0, "Email"] == "existing@example.com"
    assert "skip" in " ".join(logs).lower()


def test_fb_enrich_non_fb_email_still_attempts_and_preserves_no_email_status(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "IG Only",
            "Email": "existing@example.com",
            "Email_All": "existing@example.com",
            "facebook_url": "https://www.facebook.com/igonly",
            "Facebook_URL": "https://www.facebook.com/igonly",
            "Social Link": "",
            "External Links": "",
            "FB_Status": "pass_a_no_email_on_page",
            "FB_Attempt_State": "attempted_fb_no_email_on_page",
            "FB_Write_State": "fb_no_email_written",
            "FB_Debug_Reason": "no_email_visible",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    calls = []

    def fake_extract(driver, url, log_fn=None, **kwargs):
        calls.append(url)
        return ([], "https://www.facebook.com/igonly", "no_email_on_page")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert calls == ["https://www.facebook.com/igonly"]
    assert seed_df.at[0, "FB_Status"] == "pass_a_no_email_on_page"
    assert seed_df.at[0, "FB_Attempt_State"] == "attempted_fb"
    assert seed_df.at[0, "FB_Write_State"] == "fb_no_email_written"
    assert seed_df.at[0, "FB_Debug_Reason"] == "no_email_visible"


def test_row_has_usable_email_for_fb_skip_ignores_placeholder_only_values():
    row = pd.Series({"Email": "", "Email_All": "user@domain.com"})

    assert cde._row_has_usable_email_for_fb_skip(row) is False


def test_row_has_usable_email_for_fb_skip_treats_mixed_placeholder_and_real_as_present():
    row = pd.Series({"Email": "", "Email_All": "user@domain.com;artist@gmail.com"})

    assert cde._row_has_usable_email_for_fb_skip(row) is True


def test_fb_enrich_allows_placeholder_email_with_explicit_fb_url(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Placeholder Email",
            "Email": "user@domain.com",
            "Email_All": "user@domain.com",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            "facebook_url": "https://www.facebook.com/socialfb",
            "Facebook_URL": "https://www.facebook.com/socialfb",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    called = False

    def fake_extract(*args, **kwargs):
        nonlocal called
        called = True
        return (["real@artist.com"], "https://www.facebook.com/socialfb/about", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert called is True
    assert "real@artist.com" in seed_df.at[0, "Email_All"]


def test_fb_enrich_skips_when_fb_url_missing(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No FB URL",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    driver = object()
    discover_calls = []
    extract_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((fb_driver, artist_name, location))
        return "https://www.facebook.com/discoveredband"

    def fake_extract(driver_obj, url, log_fn=None, **kwargs):
        extract_calls.append((driver_obj, url))
        return (["fb@example.com"], "https://www.facebook.com/discoveredband/about", "")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, driver, ctx)

    assert matched is True
    assert discover_calls == [(driver, "No FB URL", "")]
    assert extract_calls == [(driver, "https://www.facebook.com/discoveredband")]
    assert seed_df.at[0, "facebook_url"] == "https://www.facebook.com/discoveredband"
    assert seed_df.at[0, "Facebook_URL"] == "https://www.facebook.com/discoveredband"
    assert seed_df.at[0, "Facebook URL"] == "https://www.facebook.com/discoveredband"
    assert any("attempting bounded discovery" in msg.lower() for msg in logs)
    assert any("candidate accepted" in msg.lower() for msg in logs)
    assert any("canonical facebook_url populated via discovery" in msg.lower() for msg in logs)


def test_fb_enrich_skips_discovery_for_unearthed_row_without_seeded_fb(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    session_gate_calls = []
    worker._ensure_fb_discovery_session = lambda driver: session_gate_calls.append(driver) or (True, "authenticated")
    seed_df = _seed_df(
        {
            "Artist Name": "Unearthed No Seed",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "",
            "External Links": "",
            "Source Directory": "Triple J Unearthed",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    monkeypatch.setattr(
        worker,
        "_discover_facebook_identity",
        lambda *args, **kwargs: discover_calls.append(args) or False,
    )
    monkeypatch.setattr(
        cde,
        "_extract_fb_emails_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("_extract_fb_emails_bounded should not run")),
    )

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert session_gate_calls == []
    assert discover_calls == []
    assert seed_df.at[0, "facebook_url"] == ""
    assert seed_df.at[0, "Facebook_URL"] == ""
    assert seed_df.at[0, "Facebook URL"] == ""
    assert any("[Unearthed Path] strict explicit-only FB mode; skipping Night FB discovery" in msg for msg in logs)


def test_fb_enrich_preserves_seeded_unearthed_fb_scrape(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    worker._ensure_fb_discovery_session = lambda driver: (True, "authenticated")
    seed_df = _seed_df(
        {
            "Artist Name": "Unearthed Seeded",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "https://www.facebook.com/unearthedseeded",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Source Directory": "Triple J Unearthed",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []
    extract_calls = []

    monkeypatch.setattr(
        worker,
        "_discover_facebook_identity",
        lambda *args, **kwargs: discover_calls.append(args) or False,
    )

    def fake_extract(driver_obj, url, log_fn=None, **kwargs):
        extract_calls.append(url)
        return (["seeded@example.com"], url, "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert discover_calls == []
    assert extract_calls == ["https://www.facebook.com/unearthedseeded"]
    assert seed_df.at[0, "Email"] == "seeded@example.com"
    assert seed_df.at[0, "Facebook_URL"] == "https://www.facebook.com/unearthedseeded"
    assert not any("[FB Discovery][Skip] Unearthed row without seeded Facebook_URL" in msg for msg in logs)


def test_fb_enrich_resolves_share_url_before_unearthed_gate(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    worker._ensure_fb_discovery_session = lambda driver: (True, "authenticated")
    worker._get_night_fb_share_promotion_resolver = lambda: (
        lambda raw: "https://www.facebook.com/unearthedsharepage"
    )
    seed_df = _seed_df(
        {
            "Artist Name": "Unearthed Share",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Source Directory": "Triple J Unearthed",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    extract_calls = []

    def fake_extract(driver_obj, url, log_fn=None, **kwargs):
        extract_calls.append(url)
        return (["share@example.com"], url, "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)
    monkeypatch.setattr(
        worker,
        "_discover_facebook_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("discovery should not run for resolved share URL")),
    )

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert extract_calls == ["https://www.facebook.com/unearthedsharepage"]
    assert seed_df.at[0, "facebook_url"] == "https://www.facebook.com/unearthedsharepage"
    assert seed_df.at[0, "Facebook_URL"] == "https://www.facebook.com/unearthedsharepage"
    assert seed_df.at[0, "Facebook URL"] == "https://www.facebook.com/unearthedsharepage"
    assert seed_df.at[0, cde.FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
    assert seed_df.at[0, cde.FB_GATE_STATE_COL] == ""
    assert not any("[Unearthed Path] strict explicit-only FB mode; skipping Night FB discovery" in msg for msg in logs)


def test_fb_enrich_disallowed_resolved_share_url_uses_bounded_runtime_fallback_for_unearthed(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    worker._ensure_fb_discovery_session = lambda driver: (True, "authenticated")
    worker._get_night_fb_share_promotion_resolver = lambda: (
        lambda raw: "https://www.facebook.com/groups/not-a-page"
    )
    seed_df = _seed_df(
        {
            "Artist Name": "Unearthed Bad Share",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
            "External Links": "",
            "Source Directory": "Triple J Unearthed",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        worker,
        "_discover_facebook_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("discovery should not run for disallowed share result")),
    )
    scrape_calls = []

    def fake_extract(_driver, url, **kwargs):  # noqa: ANN001
        scrape_calls.append(url)
        return [], url, "no_email_on_page"

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert seed_df.at[0, "facebook_url"] == ""
    assert seed_df.at[0, "Facebook_URL"] == ""
    assert seed_df.at[0, "Facebook URL"] == ""
    assert seed_df.at[0, cde.FB_OPPORTUNITY_STATE_COL] == "no_fb_opportunity"
    assert seed_df.at[0, cde.FB_GATE_STATE_COL] == ""
    assert seed_df.at[0, cde.FB_ATTEMPT_STATE_COL] == "attempted_fb"
    assert scrape_calls == ["https://www.facebook.com/share/19bactwuev"]
    assert any("state='share_runtime_fallback'" in msg for msg in logs)


def test_fb_enrich_unearthed_explicit_url_merges_with_existing_instagram_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    worker._ensure_fb_discovery_session = lambda driver: (True, "authenticated")
    seed_df = _seed_df(
        {
            "Artist Name": "Unearthed Dual Source",
            "Email": "ig@example.com",
            "Email_All": "ig@example.com",
            "Email_Source_URL": "https://www.instagram.com/uneartheddualsource/",
            "Email_Source_Type": "instagram_enrich",
            "Email_Extract_Method": "regex",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "https://www.instagram.com/uneartheddualsource/, https://www.facebook.com/uneartheddualsource",
            "External Links": "",
            "Source Directory": "Triple J Unearthed",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    _set_email_with_provenance(
        (seed_df, 0),
        "ig@example.com",
        source_url="https://www.instagram.com/uneartheddualsource/",
        source_type="instagram_enrich",
        method="regex",
        surface="instagram_profile",
    )

    extract_calls = []

    def fake_extract(driver_obj, url, log_fn=None, **kwargs):
        extract_calls.append(url)
        return (["fb@example.com"], "https://www.facebook.com/uneartheddualsource/about", "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)
    monkeypatch.setattr(
        worker,
        "_discover_facebook_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("discovery should not run for explicit Unearthed FB URL")),
    )

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert extract_calls == ["https://www.facebook.com/uneartheddualsource"]
    assert seed_df.at[0, "Email"] == "ig@example.com"
    assert set(seed_df.at[0, "Email_All"].split(";")) == {"ig@example.com", "fb@example.com"}
    provenance = json.loads(seed_df.at[0, EMAIL_PROVENANCE_JSON_COL])
    assert provenance["ig@example.com"]["source_type"] == "instagram_enrich"
    assert provenance["fb@example.com"]["source_type"] == "facebook_enrich"


def test_fb_enrich_non_unearthed_row_without_seeded_fb_still_calls_discovery(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    worker._ensure_fb_discovery_session = lambda driver: (True, "authenticated")
    seed_df = _seed_df(
        {
            "Artist Name": "Bandcamp No Seed",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "",
            "External Links": "",
            "Source Directory": "bandcamp",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    monkeypatch.setattr(
        worker,
        "_discover_facebook_identity",
        lambda *args, **kwargs: discover_calls.append(args) or False,
    )

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert len(discover_calls) == 1
    assert not any("[FB Discovery][Skip] Unearthed row without seeded Facebook_URL" in msg for msg in logs)


def test_fb_enrich_discovery_miss_preserves_no_fb_url_status(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No Match",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((artist_name, location))
        return ""

    def fail_extract(*args, **kwargs):
        raise AssertionError("_extract_fb_emails_bounded should not run after a discovery miss")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fail_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert discover_calls == [("No Match", "")]
    assert seed_df.at[0, "FB_Status"] == "no_fb_url"
    assert any("attempting bounded discovery" in msg.lower() for msg in logs)
    assert any("no safe candidate found" in msg.lower() for msg in logs)


def test_fb_enrich_discovered_url_uses_shared_accepted_page_sweep(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    worker._ensure_fb_discovery_session = lambda driver: (True, "authenticated")
    seed_df = _seed_df(
        {
            "Artist Name": "Shared Sweep Day",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "FB_Status": "",
            "FB_Reason": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    observed = {}

    monkeypatch.setattr(
        cde,
        "_discover_facebook_url_bounded",
        lambda fb_driver, artist_name, extra_signal, logger: "https://www.facebook.com/sharedsweepday",
    )

    def fake_shared_sweep(fb_url, fetch_surface, **kwargs):  # noqa: ANN001
        observed["fb_url"] = fb_url
        observed["select_secondary_url"] = kwargs.get("select_secondary_url")
        observed["fallback_secondary_urls"] = kwargs.get("fallback_secondary_urls")
        return nmfb.FacebookAcceptedPageSweepResult(
            main_surface=nmfb.FacebookAcceptedPageFetchResult(
                requested_url=fb_url,
                resolved_url=fb_url,
                html="<html><body>Bookings: shared@example.com</body></html>",
                rendered_text="Bookings: shared@example.com",
            ),
            main_emails=["shared@example.com"],
            combined_emails=["shared@example.com"],
            final_resolved_url=fb_url,
        )

    monkeypatch.setattr(cde, "_run_bounded_fb_accepted_page_sweep", fake_shared_sweep)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert observed["fb_url"] == "https://www.facebook.com/sharedsweepday"
    assert callable(observed["select_secondary_url"])
    assert callable(observed["fallback_secondary_urls"])
    assert seed_df.at[0, "Email"] == "shared@example.com"
    assert seed_df.at[0, "FB_Status"] == "found_email"
    assert seed_df.at[0, "Email_Type"] == "fb_enrich"
    assert seed_df.at[0, "Email_Source_Type"] == "facebook_enrich"
    assert seed_df.at[0, "facebook_url"] == "https://www.facebook.com/sharedsweepday"
    assert seed_df.at[0, "Facebook_URL"] == "https://www.facebook.com/sharedsweepday"


def test_fb_discovery_sets_shared_attempt_flag(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Flagged Once",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_discover_facebook_url_bounded",
        lambda *args, **kwargs: "",
    )
    monkeypatch.setattr(
        cde,
        "_extract_fb_emails_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("extract should not run after discovery miss")),
    )

    assert worker._discover_facebook_identity(seed_df, 0, object(), ctx) is False
    assert seed_df.at[0, cde.FB_DISCOVERY_ATTEMPT_FLAG_COL] == "1"


def test_missing_facebook_url_discovery_allowed_once_per_row(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "One Shot",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((artist_name, location))
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)
    monkeypatch.setattr(
        cde,
        "_extract_fb_emails_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("extract should not run after discovery miss")),
    )

    assert worker._enrich_row_facebook(seed_df, 0, object(), ctx) is False
    assert worker._enrich_row_facebook(seed_df, 0, object(), ctx) is False
    assert discover_calls == [("One Shot", "")]
    assert any("already attempted this run" in msg.lower() for msg in logs)


def test_missing_facebook_url_discovery_shared_flag_blocks_new_worker_retry(monkeypatch):
    seed_df = _seed_df(
        {
            "Artist Name": "Shared Lock",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    logs_one = []
    logs_two = []
    worker_one = _make_worker(logs_one)
    worker_two = _make_worker(logs_two)
    ctx_one = worker_one._build_row_context(seed_df, 0, 1, 1)
    ctx_two = worker_two._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((artist_name, location))
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    assert worker_one._discover_facebook_identity(seed_df, 0, object(), ctx_one) is False
    assert seed_df.at[0, cde.FB_DISCOVERY_ATTEMPT_FLAG_COL] == "1"
    assert worker_two._discover_facebook_identity(seed_df, 0, object(), ctx_two) is False
    assert discover_calls == [("Shared Lock", "")]
    assert worker_two._fb_discovery_attempted_rows == set()
    assert any("already attempted this run" in msg.lower() for msg in logs_two)


def test_discovery_fail_locks_future_retry_but_preserves_no_fb_url_status(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Locked Miss",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append(artist_name)
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    first = worker._enrich_row_facebook(seed_df, 0, object(), ctx)
    second = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert first is False
    assert second is False
    assert discover_calls == ["Locked Miss"]
    assert seed_df.at[0, "FB_Status"] == "no_fb_url"
    assert any("row will not retry discovery this run" in msg.lower() for msg in logs)
    assert any("already attempted this run" in msg.lower() for msg in logs)


def test_discovery_success_stores_url_and_second_pass_uses_explicit_url_not_discovery(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Discovered Twice",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Facebook URL": "",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []
    extract_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((artist_name, location))
        return "https://www.facebook.com/discoveredtwice"

    def fake_extract(driver_obj, url, log_fn=None, **kwargs):
        extract_calls.append(url)
        return ([], url, "no_email_on_page")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    assert worker._enrich_row_facebook(seed_df, 0, object(), ctx) is False
    assert worker._enrich_row_facebook(seed_df, 0, object(), ctx) is False
    assert discover_calls == [("Discovered Twice", "")]
    assert extract_calls == [
        "https://www.facebook.com/discoveredtwice",
        "https://www.facebook.com/discoveredtwice",
    ]
    assert seed_df.at[0, "facebook_url"] == "https://www.facebook.com/discoveredtwice"
    assert seed_df.at[0, "Facebook_URL"] == "https://www.facebook.com/discoveredtwice"


def test_explicit_facebook_url_bypasses_discovery_lock(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._fb_discovery_attempted_rows.add(0)
    seed_df = _seed_df(
        {
            "Artist Name": "Explicit Wins",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/explicitwins",
            "Facebook_URL": "https://www.facebook.com/explicitwins",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    discover_calls = []
    extract_calls = []

    monkeypatch.setattr(
        cde,
        "_discover_facebook_url_bounded",
        lambda *args, **kwargs: discover_calls.append(args) or "https://www.facebook.com/shouldnotrun",
    )

    def fake_extract(driver_obj, url, log_fn=None, **kwargs):
        extract_calls.append(url)
        return (["fb@example.com"], url, "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is True
    assert not discover_calls
    assert extract_calls == ["https://www.facebook.com/explicitwins"]
    assert all("already attempted this run" not in msg.lower() for msg in logs)


def test_fb_discovery_lock_does_not_leak_across_worker_runs(monkeypatch):
    seed_one = _seed_df(
        {
            "Artist Name": "Fresh Worker",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    seed_two = seed_one.copy()
    logs_one = []
    logs_two = []
    worker_one = _make_worker(logs_one)
    worker_two = _make_worker(logs_two)
    ctx_one = worker_one._build_row_context(seed_one, 0, 1, 1)
    ctx_two = worker_two._build_row_context(seed_two, 0, 1, 1)

    discover_calls = []

    def fake_discover(fb_driver, artist_name, location, logger):
        discover_calls.append((artist_name, id(logger)))
        return ""

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", fake_discover)

    assert worker_one._enrich_row_facebook(seed_one, 0, object(), ctx_one) is False
    assert worker_two._enrich_row_facebook(seed_two, 0, object(), ctx_two) is False
    assert len(discover_calls) == 2
    assert worker_one._fb_discovery_attempted_rows == {0}
    assert worker_two._fb_discovery_attempted_rows == {0}


def test_fb_discovery_skips_when_shared_session_not_authenticated(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._fb_session_auth_checked = False
    worker._fb_session_authenticated = False
    seed_df = _seed_df(
        {
            "Artist Name": "Needs Auth",
            "Email": "",
            "Email_All": "",
            "facebook_url": "",
            "Facebook_URL": "",
            "Social Link": "",
            "External Links": "",
            "FB_Status": "",
            "FB_Reason": "",
            "Location": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(cde, "_probe_fb_session_state", lambda driver, visit_home: (False, "redirect_login"))

    discover_calls = []
    monkeypatch.setattr(
        cde,
        "_discover_facebook_url_bounded",
        lambda *args, **kwargs: discover_calls.append(args) or "https://www.facebook.com/should-not-run",
    )

    matched = worker._discover_facebook_identity(seed_df, 0, object(), ctx)

    assert matched is False
    assert discover_calls == []
    assert worker._fb_discovery_disabled is True
    assert worker._fb_discovery_disabled_reason == "redirect_login"
    assert seed_df.at[0, "FB_Status"] == "fb_discovery_disabled"
    assert seed_df.at[0, "FB_Reason"] == "redirect_login"


def test_ensure_fb_discovery_session_logs_authenticated_once(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._fb_session_auth_checked = False
    worker._fb_session_authenticated = False
    worker._fb_session_auth_reason = ""

    monkeypatch.setattr(cde, "_probe_fb_session_state", lambda driver, visit_home: (True, "authenticated"))

    assert worker._ensure_fb_discovery_session(object()) == (True, "authenticated")
    assert any("Shared Facebook session authenticated" in message for message in logs)

    log_count = len(logs)
    assert worker._ensure_fb_discovery_session(object()) == (True, "authenticated")
    assert len(logs) == log_count


def test_night_mode_fb_discovery_uses_canonical_checkpoint_decision(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker.night_mode = True
    worker._fb_session_auth_checked = False
    worker._fb_session_authenticated = False
    worker._fb_session_auth_reason = ""

    monkeypatch.setattr(
        cde,
        "probe_night_fb_session_decision",
        lambda driver, visit_home: SimpleNamespace(
            state="authenticated_but_checkpointed",
            reason="checkpoint",
            authenticated=True,
            usable=False,
        ),
    )

    assert worker._ensure_fb_discovery_session(object()) == (False, "checkpoint")
    assert worker._fb_discovery_disabled is True
    assert worker._fb_discovery_disabled_reason == "checkpoint"
    assert any("reason=checkpoint" in message for message in logs)


def test_night_mode_fb_discovery_respects_shared_run_disable(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker.night_mode = True
    worker.night_fb_run_state = nmfb.create_night_fb_run_state("user", "pass")
    nmfb.disable_night_fb_run_state(
        worker.night_fb_run_state,
        "checkpoint",
        checkpointed=True,
    )
    probe_calls = []

    monkeypatch.setattr(
        cde,
        "probe_night_fb_session_decision",
        lambda driver, visit_home: probe_calls.append((driver, visit_home)),
    )

    assert worker._ensure_fb_discovery_session(object()) == (False, "checkpoint")
    assert probe_calls == []
    assert worker._fb_discovery_disabled is True
    assert worker._fb_discovery_disabled_reason == "checkpoint"


def test_ensure_fb_discovery_session_runs_warmup_once(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._fb_session_auth_checked = False
    worker._fb_session_authenticated = False
    worker._fb_session_warmup_complete = False

    class _Driver:
        def __init__(self) -> None:
            self.loaded_urls = []
            self.scripts = []

        def get(self, url):  # noqa: ANN001
            self.loaded_urls.append(url)

        def execute_script(self, script):  # noqa: ANN001
            self.scripts.append(script)

    driver = _Driver()
    sleep_calls = []

    monkeypatch.setattr(cde, "_probe_fb_session_state", lambda driver, visit_home: (True, "authenticated"))
    monkeypatch.setattr(cde.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    def fake_uniform(low, high):
        if (low, high) == (3.0, 6.0):
            return 3.5
        if (low, high) == (1.0, 2.0):
            return 1.5
        raise AssertionError(f"unexpected range: {(low, high)}")

    monkeypatch.setattr(cde.random, "uniform", fake_uniform)

    assert worker._ensure_fb_discovery_session(driver) == (True, "authenticated")
    assert worker._ensure_fb_discovery_session(driver) == (True, "authenticated")

    assert worker._fb_session_warmup_complete is True
    assert driver.loaded_urls == ["https://www.facebook.com/"]
    assert driver.scripts == ["window.scrollBy(0, 180);"]
    assert sleep_calls == [3.5, 1.5]
    assert logs.count("[FB Warmup] Running Facebook session warm-up") == 1


def test_fb_enrich_explicit_url_routes_through_fb_session_gate(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Warmup Gate",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/warmupgate",
            "Facebook_URL": "https://www.facebook.com/warmupgate",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    driver = object()
    gate_calls = []

    monkeypatch.setattr(
        worker,
        "_ensure_fb_discovery_session",
        lambda fb_driver, force=False: gate_calls.append((fb_driver, force)) or (True, "authenticated"),
    )
    monkeypatch.setattr(
        cde,
        "_extract_fb_emails_bounded",
        lambda fb_driver, url, log_fn=None, **kwargs: (["fb@example.com"], url, ""),
    )

    assert worker._enrich_row_facebook(seed_df, 0, driver, ctx) is True
    assert gate_calls == [(driver, False)]


def test_night_mode_fb_enrich_threads_shared_session_into_bounded_extract(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker.night_mode = True
    worker._row_allows_heavy_enricher = lambda *args, **kwargs: SimpleNamespace(allowed=True)
    worker._ensure_fb_discovery_session = lambda fb_driver, force=False: (True, "authenticated")
    shared_session = object()
    worker.night_fb_run_state = SimpleNamespace(session=shared_session)
    seed_df = _seed_df(
        {
            "Artist Name": "Night Session Artist",
            "Email": "",
            "Email_All": "",
            "facebook_url": "https://www.facebook.com/nightsessionartist",
            "Facebook_URL": "https://www.facebook.com/nightsessionartist",
            "Social Link": "",
            "External Links": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "FB_Status": "",
            "FB_Reason": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    observed = {}

    def fake_extract(fb_driver, url, log_fn=None, fb_session=None, **kwargs):  # noqa: ANN001
        observed["fb_driver"] = fb_driver
        observed["url"] = url
        observed["fb_session"] = fb_session
        return (["night@example.com"], url, "")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    assert worker._enrich_row_facebook(seed_df, 0, object(), ctx) is True
    assert observed["url"] == "https://www.facebook.com/nightsessionartist"
    assert observed["fb_session"] is shared_session
    assert seed_df.at[0, "Email"] == "night@example.com"


def test_run_impl_disables_fb_discovery_when_auth_cookie_missing(tmp_path, monkeypatch):
    logs = []
    seed_csv = tmp_path / "missing.csv"
    output_csv = tmp_path / "output.csv"
    worker = cde.CrossDirectoryEnricherWorker(seed_csv.as_posix(), output_csv.as_posix(), enable_live_search=False)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    worker.progress = SimpleNamespace(emit=lambda *args, **kwargs: None)
    worker.finished = SimpleNamespace(emit=lambda *args, **kwargs: None)

    class _Driver:
        def __init__(self) -> None:
            self.loaded_urls = []

        def get(self, url):  # noqa: ANN001
            self.loaded_urls.append(url)

        def get_cookie(self, name):  # noqa: ANN001
            return None

        def execute_cdp_cmd(self, command, payload):  # noqa: ANN001
            return {"cookies": []}

    driver = _Driver()
    monkeypatch.setattr(cde, "_get_enricher_facebook_driver", lambda: driver)
    monkeypatch.setattr(cde, "_cleanup_enricher_facebook_driver", lambda: None)
    monkeypatch.setattr(cde, "enricher_fb_profile_has_cookies", lambda: True)

    worker._run_impl()

    assert worker._fb_discovery_disabled is True
    assert worker._fb_discovery_disabled_reason == "not_authenticated"
    assert driver.loaded_urls == []
    assert any("reason=not_authenticated" in message for message in logs)


def test_fb_discovery_disables_remaining_run_after_invalid_session(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker._fb_session_auth_checked = False
    worker._fb_session_authenticated = False
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Dead Session A",
                "Email": "",
                "Email_All": "",
                "facebook_url": "",
                "Facebook_URL": "",
                "Social Link": "",
                "External Links": "",
                "FB_Status": "",
                "FB_Reason": "",
                "Location": "",
            },
            {
                "Artist Name": "Dead Session B",
                "Email": "",
                "Email_All": "",
                "facebook_url": "",
                "Facebook_URL": "",
                "Social Link": "",
                "External Links": "",
                "FB_Status": "",
                "FB_Reason": "",
                "Location": "",
            },
        ],
        dtype=str,
    ).fillna("")
    ctx_one = worker._build_row_context(seed_df, 0, 1, 2)
    ctx_two = worker._build_row_context(seed_df, 1, 2, 2)

    monkeypatch.setattr(cde, "_probe_fb_session_state", lambda driver, visit_home: (True, "authenticated"))

    discover_calls = {"count": 0}

    def _raise_invalid_session(*args, **kwargs):
        discover_calls["count"] += 1
        raise cde.InvalidSessionIdException("invalid session id")

    monkeypatch.setattr(cde, "_discover_facebook_url_bounded", _raise_invalid_session)

    assert worker._discover_facebook_identity(seed_df, 0, object(), ctx_one) is False
    assert worker._discover_facebook_identity(seed_df, 1, object(), ctx_two) is False
    assert discover_calls["count"] == 1
    assert worker._fb_discovery_disabled is True
    assert worker._fb_session_invalid is True
    assert seed_df.at[0, "FB_Status"] == "fb_discovery_disabled"
    assert seed_df.at[0, "FB_Reason"] == "session_invalid"
    assert seed_df.at[1, "FB_Status"] == "fb_discovery_disabled"
    assert seed_df.at[1, "FB_Reason"] == "session_invalid"


def test_phase_facebook_reuses_indexed_domain_email_without_second_scrape(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Bright One",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
                "facebook_url": "https://www.facebook.com/brightone",
                "Facebook_URL": "https://www.facebook.com/brightone",
                "Facebook URL": "https://www.facebook.com/brightone",
                "Social Link": "",
                "External Links": "",
            },
            {
                "Artist Name": "Bright Two",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
                "facebook_url": "https://www.facebook.com/brighttwo",
                "Facebook_URL": "https://www.facebook.com/brighttwo",
                "Facebook URL": "https://www.facebook.com/brighttwo",
                "Social Link": "",
                "External Links": "",
            },
        ],
        dtype=str,
    ).fillna("")

    extract_calls = []

    def fake_extract(driver_obj, url, log_fn=None, **kwargs):
        extract_calls.append(url)
        if url == "https://www.facebook.com/brightone":
            return (["mgmt@brightmusic.com"], url, "")
        raise AssertionError("second row should reuse indexed domain email before FB scrape")

    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fake_extract)

    worker._phase_facebook(seed_df, object(), total=2)

    assert extract_calls == ["https://www.facebook.com/brightone"]
    assert seed_df.at[1, "Email"] == "mgmt@brightmusic.com"
    assert seed_df.at[1, "Email_All"] == "mgmt@brightmusic.com"
    assert seed_df.at[1, "Email_Type"] == "fb_enrich"
    assert seed_df.at[1, "Email_Source_URL"] == "https://www.facebook.com/brightone"
    assert seed_df.at[1, "Email_Source_Type"] == "facebook_enrich"
    assert seed_df.at[1, "Email_Extract_Method"] == "regex"
    assert len(worker._domain_email_reuse_index) == 1
    assert worker._domain_email_reuse_index["brightmusic.com"]["role"] == "management"
    assert worker._domain_email_reuse_count == 1


def test_domain_email_reuse_only_fills_rows_without_email():
    logs = []
    worker = _make_worker(logs)
    worker._index_domain_email_reuse(
        "brightmusic.com",
        email="mgmt@brightmusic.com",
        email_all="mgmt@brightmusic.com",
        source_url="https://www.facebook.com/brightone",
        source_type="facebook_enrich",
        extract_method="regex",
        email_type="fb_enrich",
    )
    seed_df = _seed_df(
        {
            "Artist Name": "Already Filled",
            "Email": "existing@brightmusic.com",
            "Email_All": "existing@brightmusic.com",
            "Email_Type": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Spotify_Website_URL": "https://brightmusic.com",
        }
    )

    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    reused = worker._maybe_apply_domain_email_reuse(seed_df, 0, ctx)

    assert reused is False
    assert seed_df.at[0, "Email"] == "existing@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "existing@brightmusic.com"
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_skips_when_only_email_all_is_populated():
    logs = []
    worker = _make_worker(logs)
    worker._index_domain_email_reuse(
        "brightmusic.com",
        email="mgmt@brightmusic.com",
        email_all="mgmt@brightmusic.com",
        source_url="https://www.facebook.com/brightone",
        source_type="facebook_enrich",
        extract_method="regex",
        email_type="fb_enrich",
    )
    seed_df = _seed_df(
        {
            "Artist Name": "Email All Only",
            "Email": "",
            "Email_All": "existing@brightmusic.com",
            "Email_Type": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Spotify_Website_URL": "https://brightmusic.com",
        }
    )

    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    reused = worker._maybe_apply_domain_email_reuse(seed_df, 0, ctx)

    assert reused is False
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == "existing@brightmusic.com"
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_propagates_to_earlier_blank_same_domain_row():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Earlier Blank",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True

    assert seed_df.at[0, "Email"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_Type"] == "fb_enrich"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.facebook.com/brightone"
    assert seed_df.at[0, "Email_Source_Type"] == "facebook_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[1, "Email"] == "mgmt@brightmusic.com"
    assert worker._domain_email_reuse_count == 1
    assert worker._yield_tracker.counts["domain_reuse"] == 1


def test_domain_email_reuse_propagation_does_not_overwrite_existing_email():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Existing Email",
                "Email": "existing@brightmusic.com",
                "Email_All": "existing@brightmusic.com",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True

    assert seed_df.at[0, "Email"] == "existing@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "existing@brightmusic.com"
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_propagation_skips_email_all_only_rows():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Email All Only",
                "Email": "",
                "Email_All": "existing@brightmusic.com",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True

    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == "existing@brightmusic.com"
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_propagation_skips_different_domains():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Other Domain",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://othermusic.com/about",
            },
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True

    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert worker._domain_email_reuse_count == 0


def test_domain_email_reuse_propagation_is_idempotent_for_repeated_same_email():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Earlier Blank",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "First Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
            {
                "Artist Name": "Repeated Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brighttwo",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/team",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is True
    assert worker._domain_email_reuse_count == 1

    assert worker._index_domain_email_reuse_from_row(seed_df, 2, "brightmusic.com") is False

    assert seed_df.at[0, "Email"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "mgmt@brightmusic.com"
    assert worker._domain_email_reuse_count == 1
    assert worker._yield_tracker.counts["domain_reuse"] == 1


def test_domain_email_reuse_index_is_worker_local():
    logs_one = []
    logs_two = []
    worker_one = _make_worker(logs_one)
    worker_two = _make_worker(logs_two)

    assert worker_one._index_domain_email_reuse(
        "brightmusic.com",
        email="mgmt@brightmusic.com",
        email_all="mgmt@brightmusic.com;mgmt@brightmusic.com",
        source_url="https://www.facebook.com/brightone",
        source_type="facebook_enrich",
        extract_method="regex",
        email_type="fb_enrich",
    )

    seed_one = _seed_df(
        {
            "Artist Name": "Worker One",
            "Email": "",
            "Email_All": "",
            "Email_Type": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Spotify_Website_URL": "https://brightmusic.com",
        }
    )
    seed_two = seed_one.copy()

    ctx_one = worker_one._build_row_context(seed_one, 0, 1, 1)
    ctx_two = worker_two._build_row_context(seed_two, 0, 1, 1)

    assert worker_one._maybe_apply_domain_email_reuse(seed_one, 0, ctx_one) is True
    assert worker_two._maybe_apply_domain_email_reuse(seed_two, 0, ctx_two) is False
    assert seed_one.at[0, "Email"] == "mgmt@brightmusic.com"
    assert seed_one.at[0, "Email_All"] == "mgmt@brightmusic.com"
    assert seed_two.at[0, "Email"] == ""
    assert seed_two.at[0, "Email_All"] == ""
    assert worker_one._domain_email_reuse_index["brightmusic.com"]["role"] == "management"


def test_seed_directory_email_rows_are_not_indexed_for_domain_reuse():
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Seed Directory Artist",
            "Email": "seed@brightmusic.com",
            "Email_All": "seed@brightmusic.com",
            "Email_Type": "",
            "Email_Source_URL": "https://brightmusic.com/contact",
            "Email_Source_Type": "",
            "Email_Extract_Method": "regex",
            "Email Source": "Seed directory (site/email scrape)",
            "Spotify_Website_URL": "https://brightmusic.com",
        }
    )

    indexed = worker._index_domain_email_reuse_from_row(
        seed_df,
        0,
        "brightmusic.com",
        source_label="Seed directory (site/email scrape)",
    )

    assert indexed is False
    assert worker._domain_email_reuse_index == {}
    assert worker._domain_profile_index == {}


def test_late_domain_backfill_fills_earlier_empty_rows_without_fetches(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Earlier Empty",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "Keep Existing Email",
                "Email": "existing@brightmusic.com",
                "Email_All": "existing@brightmusic.com",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
            {
                "Artist Name": "Keep Existing Email All",
                "Email": "",
                "Email_All": "keep@brightmusic.com",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/press",
            },
        ],
        dtype=str,
    ).fillna("")
    discovery_df = pd.DataFrame(
        [
            {
                "Artist Name": "Later Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/home",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(discovery_df, 0, "brightmusic.com") is True

    def fail(*args, **kwargs):
        raise AssertionError("late-run backfill should not touch enrichment/fetch paths")

    monkeypatch.setattr(worker, "_enrich_row_sc_live", fail)
    monkeypatch.setattr(worker, "_enrich_row_live_lookup", fail)
    monkeypatch.setattr(worker, "_enrich_row_instagram_email", fail)
    monkeypatch.setattr(worker, "_enrich_row_website_email", fail)
    monkeypatch.setattr(worker, "_enrich_row_facebook", fail)

    stats = worker._run_late_domain_email_backfill(seed_df, total=4)

    assert stats == {
        "rows_scanned": 3,
        "rows_eligible": 1,
        "rows_backfilled": 1,
        "rows_skipped": 2,
    }
    assert seed_df.at[0, "Email"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_Type"] == "fb_enrich"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.facebook.com/brightone"
    assert seed_df.at[0, "Email_Source_Type"] == "facebook_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[1, "Email"] == "existing@brightmusic.com"
    assert seed_df.at[1, "Email_All"] == "existing@brightmusic.com"
    assert seed_df.at[2, "Email"] == ""
    assert seed_df.at[2, "Email_All"] == "keep@brightmusic.com"
    assert worker._domain_email_reuse_count == 1
    assert worker._yield_tracker.counts["domain_reuse"] == 1
    assert any("Late domain reuse backfill" in msg for msg in logs)


def test_late_domain_backfill_applies_best_ranked_contact_and_preserves_all_same_domain_contacts(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Earlier Empty",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
        ],
        dtype=str,
    ).fillna("")
    discovery_df = pd.DataFrame(
        [
            {
                "Artist Name": "Generic Discovery",
                "Email": "info@brightmusic.com",
                "Email_All": "info@brightmusic.com",
                "Email_Type": "website_enrich",
                "Email_Source_URL": "https://brightmusic.com/contact",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
            {
                "Artist Name": "Better Discovery",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com;info@brightmusic.com",
                "Email_Type": "fb_enrich",
                "Email_Source_URL": "https://www.facebook.com/brightone",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Spotify_Website_URL": "https://brightmusic.com/home",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(discovery_df, 0, "brightmusic.com") is True
    assert worker._index_domain_email_reuse_from_row(discovery_df, 1, "brightmusic.com") is True

    def fail(*args, **kwargs):
        raise AssertionError("late-run backfill should not touch enrichment/fetch paths")

    monkeypatch.setattr(worker, "_enrich_row_sc_live", fail)
    monkeypatch.setattr(worker, "_enrich_row_live_lookup", fail)
    monkeypatch.setattr(worker, "_enrich_row_instagram_email", fail)
    monkeypatch.setattr(worker, "_enrich_row_website_email", fail)
    monkeypatch.setattr(worker, "_enrich_row_facebook", fail)

    stats = worker._run_late_domain_email_backfill(seed_df, total=3)

    assert stats == {
        "rows_scanned": 1,
        "rows_eligible": 1,
        "rows_backfilled": 1,
        "rows_skipped": 0,
    }
    assert seed_df.at[0, "Email"] == "mgmt@brightmusic.com"
    assert pipeline_runner.normalize_emails(seed_df.at[0, "Email_All"]) == [
        "info@brightmusic.com",
        "mgmt@brightmusic.com",
    ]
    assert seed_df.at[0, "Email_Type"] == "fb_enrich"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.facebook.com/brightone"
    assert seed_df.at[0, "Email_Source_Type"] == "facebook_enrich"


def test_fb_enrich_rejects_invalid_discovered_candidate(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Invalid Candidate",
            "Email": "",
            "Email_All": "",
            "Social Link": "",
            "External Links": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_find_best_page(
        artist_name,
        location,
        fb_client,
        logger,
        require_strong_candidate=False,
        skip_login_check=False,
    ):
        return "https://www.facebook.com/share.php?u=bad"

    def fail_extract(*args, **kwargs):
        raise AssertionError("_extract_fb_emails_bounded should not run for an invalid discovered candidate")

    monkeypatch.setattr(cde, "facebook_find_best_page", fake_find_best_page)
    monkeypatch.setattr(cde, "_extract_fb_emails_bounded", fail_extract)

    matched = worker._enrich_row_facebook(seed_df, 0, object(), ctx)

    assert matched is False
    assert seed_df.at[0, "FB_Status"] == "no_fb_url"
    assert seed_df.at[0, "facebook_url"] == ""
    assert seed_df.at[0, "Facebook_URL"] == ""
    assert any("no safe candidate found" in msg.lower() for msg in logs)


def test_cross_directory_promotion_backfills_canonical_from_lower_alias():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Lower Alias",
                "facebook_url": "https://www.facebook.com/existinglower",
                "Facebook_URL": "",
                "Social Link": "",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")

    promoted = cde._apply_fb_promotion_df(df.copy())

    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/existinglower"
