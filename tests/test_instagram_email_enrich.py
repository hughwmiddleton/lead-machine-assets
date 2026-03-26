from contextlib import contextmanager
import pandas as pd
import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde


def _make_worker(logs):
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    worker._set_platform_state = lambda *args, **kwargs: None
    return worker


def _seed_df(row):
    return pd.DataFrame([row], dtype=str).fillna("")


@pytest.fixture(autouse=True)
def _disable_real_instagram_live_bridge(monkeypatch):
    monkeypatch.setattr(cde, "_open_instagram_live_page_bridge", lambda *args, **kwargs: None)


class _DummyClosable:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _DummyInstagramHiddenContactPage(_DummyClosable):
    def __init__(self, html, *, candidates=None, click_effects=None):
        super().__init__()
        self._html = html
        self._candidates = list(candidates or [])
        self._click_effects = dict(click_effects or {})
        self.click_calls = []
        self.wait_calls = []

    def content(self):
        return self._html

    def evaluate(self, script):  # noqa: ANN001
        return [
            {
                "selector": candidate.get(
                    "selector",
                    f'[data-ig-hidden-contact="ig-hidden-contact-{idx}"]',
                ),
                "dom_index": candidate.get("dom_index", idx),
                "text": candidate.get("text", ""),
                "aria_label": candidate.get("aria_label", ""),
                "title": candidate.get("title", ""),
                "value": candidate.get("value", ""),
            }
            for idx, candidate in enumerate(self._candidates)
        ]

    def click(self, selector, *args, **kwargs):  # noqa: ANN002, ANN003
        self.click_calls.append(selector)
        effect = self._click_effects.get(selector)
        if callable(effect):
            effect(self)
        elif isinstance(effect, str):
            self._html = effect

    def wait_for_timeout(self, timeout_ms):  # noqa: ANN001
        self.wait_calls.append(timeout_ms)


def _make_instagram_live_bridge(page):
    return cde.InstagramLivePageBridge(
        playwright=_DummyClosable(),
        browser=_DummyClosable(),
        context=_DummyClosable(),
        page=page,
    )


def _install_instagram_profile_fetch_scope(
    monkeypatch,
    *,
    static_html,
    live_page_factory,
    static_status=200,
):
    live_pages = []

    @contextmanager
    def fake_scope(session, url, retain_live_page=False):  # noqa: ANN001
        if retain_live_page:
            page = live_page_factory()
            live_pages.append(page)
            result = cde.InstagramProfileFetchResult(
                html=page.content(),
                status=200,
                live_page=_make_instagram_live_bridge(page),
            )
        else:
            result = cde.InstagramProfileFetchResult(html=static_html, status=static_status)
        try:
            yield result
        finally:
            if result.live_page is not None:
                result.live_page.close()

    monkeypatch.setattr(cde, "_instagram_profile_fetch_scope", fake_scope)
    monkeypatch.setattr(
        cde,
        "_open_instagram_live_page_bridge",
        lambda *args, **kwargs: live_pages.append(live_page_factory()) or _make_instagram_live_bridge(live_pages[-1]),
    )
    return live_pages


def test_fetch_instagram_profile_html_uses_shared_fallback_for_unusable_initial_response(monkeypatch):
    session_calls = []
    fetch_html_calls = []

    class DummySession:
        def get(self, url, timeout=None, allow_redirects=None):  # noqa: ANN001
            session_calls.append((url, timeout, allow_redirects))
            return SimpleNamespace(status_code=200, text="<html><body>short</body></html>")

    def fake_fetch_html(url, **kwargs):  # noqa: ANN001
        fetch_html_calls.append((url, kwargs))
        return {
            "status": 200,
            "html": (
                "<html><head><meta property='og:description' content='Bookings: bookings@artist.com'></head>"
                "<body><div>Rendered profile</div></body></html>"
            ),
            "mode_used": "playwright",
            "reason": "missing_selectors",
        }

    monkeypatch.setattr(cde, "fetch_html", fake_fetch_html)

    html, status = cde._fetch_instagram_profile_html(DummySession(), "https://www.instagram.com/igartist/")

    assert status == 200
    assert "bookings@artist.com" in html
    assert session_calls == [("https://www.instagram.com/igartist/", cde.HTTP_TIMEOUT, False)]
    assert len(fetch_html_calls) == 1
    fallback_url, fallback_kwargs = fetch_html_calls[0]
    assert fallback_url == "https://www.instagram.com/igartist/"
    assert isinstance(fallback_kwargs["session"], DummySession)
    assert fallback_kwargs["directory"] == "instagram"
    assert fallback_kwargs["required_selectors"] == [cde._INSTAGRAM_REQUIRED_SELECTOR]
    assert fallback_kwargs["allow_browser_fallback"] is True
    assert fallback_kwargs["timeout_s"] == cde.HTTP_TIMEOUT


def test_instagram_profile_fetch_scope_static_mode_does_not_open_live_page(monkeypatch):
    class DummySession:
        def get(self, url, timeout=None, allow_redirects=None):  # noqa: ANN001
            return SimpleNamespace(
                status_code=200,
                text=(
                    "<html><head><meta property='og:description' content='Bookings: bookings@artist.com'></head>"
                    "<body><div>Official profile</div></body></html>"
                ),
            )

    monkeypatch.setattr(
        cde,
        "_open_instagram_live_page_bridge",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live bridge should not open")),
    )

    with cde._instagram_profile_fetch_scope(
        DummySession(),
        "https://www.instagram.com/igartist/",
        retain_live_page=False,
    ) as result:
        assert result.status == 200
        assert "bookings@artist.com" in result.html
        assert result.live_page is None


def test_instagram_profile_fetch_scope_bridge_returns_live_page_and_closes_it(monkeypatch):
    class DummySession:
        def get(self, url, timeout=None, allow_redirects=None):  # noqa: ANN001
            return SimpleNamespace(
                status_code=200,
                text=(
                    "<html><head><meta property='og:description' content='Official profile'></head>"
                    "<body><div>No email yet</div></body></html>"
                ),
            )

    class DummyClosable:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class DummyPage(DummyClosable):
        def __init__(self, html):
            super().__init__()
            self._html = html
            self.click_calls = 0

        def content(self):
            return self._html

        def click(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.click_calls += 1

    bridge = cde.InstagramLivePageBridge(
        playwright=DummyClosable(),
        browser=DummyClosable(),
        context=DummyClosable(),
        page=DummyPage(
            "<html><head><meta property='og:description' content='Official live profile'></head>"
            "<body><div>Rendered profile</div></body></html>"
        ),
    )

    monkeypatch.setattr(cde, "_open_instagram_live_page_bridge", lambda *args, **kwargs: bridge)

    with cde._instagram_profile_fetch_scope(
        DummySession(),
        "https://www.instagram.com/igartist/",
        retain_live_page=True,
    ) as result:
        assert result.status == 200
        assert "Official live profile" in result.html
        assert result.live_page is bridge
        assert result.live_page.page.click_calls == 0
        assert result.live_page.closed is False

    assert bridge.closed is True
    assert bridge.page.closed is True
    assert bridge.context.closed is True
    assert bridge.browser.closed is True
    assert bridge.playwright.closed is True


def test_instagram_email_falls_back_when_requests_html_has_no_profile_signal(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker.session = SimpleNamespace(
        get=lambda url, timeout=None, allow_redirects=None: SimpleNamespace(
            status_code=200,
            text="<html><body>Official profile shell with no contact details loaded yet.</body></html>",
        )
    )
    seed_df = _seed_df(
        {
            "Artist Name": "IG Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/igartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    fetch_html_calls = []

    def fake_fetch_html(url, **kwargs):  # noqa: ANN001
        fetch_html_calls.append((url, kwargs))
        return {
            "status": 200,
            "html": (
                "<html><head><meta property='og:description' content='Bookings: bookings@artist.com'></head>"
                "<body><div>Rendered profile</div></body></html>"
            ),
            "mode_used": "playwright",
            "reason": "missing_selectors",
        }

    monkeypatch.setattr(cde, "fetch_html", fake_fetch_html)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(fetch_html_calls) == 1
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/igartist/",
        "[IG Email] Found email: bookings@artist.com",
    ]


def test_instagram_email_keeps_requests_fast_path_when_requests_html_already_has_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker.session = SimpleNamespace(
        get=lambda url, timeout=None, allow_redirects=None: SimpleNamespace(
            status_code=200,
            text=(
                "<html><head><meta property='og:description' content='Bookings: bookings@artist.com'></head>"
                "<body><div>Official profile</div></body></html>"
            ),
        )
    )
    seed_df = _seed_df(
        {
            "Artist Name": "IG Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/igartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "fetch_html",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/igartist/",
        "[IG Email] Found email: bookings@artist.com",
    ]


def test_instagram_email_no_email_visible_after_requests_and_fallback_are_exhausted(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    worker.session = SimpleNamespace(
        get=lambda url, timeout=None, allow_redirects=None: SimpleNamespace(
            status_code=200,
            text="<html><body>Official profile shell with no contact details loaded yet.</body></html>",
        )
    )
    seed_df = _seed_df(
        {
            "Artist Name": "No Email Here",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://www.instagram.com/noemailhere/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    before = seed_df.copy(deep=True)
    fetch_html_calls = []

    def fake_fetch_html(url, **kwargs):  # noqa: ANN001
        fetch_html_calls.append((url, kwargs))
        return {
            "status": 200,
            "html": (
                "<html><head><meta property='og:description' content='Official profile'></head>"
                "<body><a href='https://example.com/contact'>Contact</a></body></html>"
            ),
            "mode_used": "playwright",
            "reason": "missing_selectors",
        }

    monkeypatch.setattr(cde, "fetch_html", fake_fetch_html)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert len(fetch_html_calls) == 1
    assert seed_df.equals(before)
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/noemailhere/",
        "[IG Email] no_email_visible",
    ]


def test_instagram_email_meta_description_writes_email_and_provenance(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "IG Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/igartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    fetch_calls = []

    def fake_fetch(session, url):
        fetch_calls.append(url)
        return (
            "<html><head><meta property='og:description' content='Bookings: bookings@artist.com'></head>"
            "<body><div>Official profile</div></body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert fetch_calls == ["https://www.instagram.com/igartist/"]
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.instagram.com/igartist/"
    assert seed_df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "ig_enrich"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/igartist/",
        "[IG Email] Found email: bookings@artist.com",
    ]


def test_instagram_email_filters_malformed_candidates_before_write(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "IG Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/igartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_fetch(session, url):  # noqa: ANN001
        return (
            "<html><head>"
            "<meta property='og:description' content='and@cultartists.may bookings@artist.com +@lover.wav'>"
            "</head><body><div>with@dill.flamenco</div></body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/igartist/",
        "[IG Email] Found email: bookings@artist.com",
    ]


def test_instagram_email_body_scan_still_writes_email_and_provenance(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "IG Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/igartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    fetch_calls = []

    def fake_fetch(session, url):
        fetch_calls.append(url)
        return ("<html><body>Bookings: bookings@artist.com</body></html>", 200)

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert fetch_calls == ["https://www.instagram.com/igartist/"]
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.instagram.com/igartist/"
    assert seed_df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "ig_enrich"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/igartist/",
        "[IG Email] Found email: bookings@artist.com",
    ]


def test_instagram_email_filters_telemetry_only_result(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "IG Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/igartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_fetch(session, url):
        return ("<html><body>abc@o363271.ingest.us.sentry.io</body></html>", 200)

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/igartist/",
        "[IG Email] no_email_visible",
    ]


@pytest.mark.parametrize(
    "row",
    [
        {
            "Artist Name": "Has Email",
            "Email": "existing@example.com",
            "Email_All": "existing@example.com",
            "Instagram_URL": "https://www.instagram.com/igartist/",
        },
        {
            "Artist Name": "Bad URL",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://www.instagram.com/p/abc123/",
        },
    ],
)
def test_instagram_email_negative_skips_without_fetch(monkeypatch, row):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    for key, value in row.items():
        seed_df.at[0, key] = value
    before = seed_df.copy(deep=True)
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fetch should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.equals(before)
    assert logs == []


def test_instagram_email_no_visible_or_meta_email_keeps_one_hop_bounded_and_no_extract(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No Email Here",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://www.instagram.com/noemailhere/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    before = seed_df.copy(deep=True)

    ig_fetch_calls = []
    bio_fetch_calls = []

    def fake_fetch(session, url):
        ig_fetch_calls.append(url)
        return (
            "<html><head><meta property='og:description' content='Official profile'>"
            "<meta name='description' content='Music artist'></head><body><a href='https://example.com/contact'>Contact</a>"
            "<a href='https://linktr.ee/noemailhere'>Linktree</a></body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            html="<html><body><a href='https://example.com/contact'>Contact</a></body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert ig_fetch_calls == ["https://www.instagram.com/noemailhere/"]
    assert bio_fetch_calls == ["https://linktr.ee/noemailhere"]
    assert seed_df.equals(before)
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/noemailhere/",
        "[IG Email] no_email_visible",
    ]


def test_instagram_email_fetch_failed_is_logged_distinctly(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Fetch Failed",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://www.instagram.com/fetchfailed/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    before = seed_df.copy(deep=True)

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", lambda session, url: ("", 503))

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.equals(before)
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/fetchfailed/",
        "[IG Email] fetch_failed status=503",
    ]


def test_instagram_email_blocked_or_empty_is_logged_distinctly(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Blocked",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://www.instagram.com/blockedartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    before = seed_df.copy(deep=True)

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda session, url: ("<html><body>Enable JavaScript to continue</body></html>", 200),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.equals(before)
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/blockedartist/",
        "[IG Email] blocked_or_empty status=200 chars=55",
    ]


def test_instagram_email_extracts_href_only_email_and_preserves_writeback(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Href Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/hrefartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_fetch(session, url):
        return (
            "<html><head><meta property='og:description' content='Official profile'></head>"
            "<body><a href='https://example.com/contact?email=press%40artist.com'>Contact</a></body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "press@artist.com"
    assert seed_df.at[0, "Email_All"] == "press@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.instagram.com/hrefartist/"
    assert seed_df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "ig_enrich"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/hrefartist/",
        "[IG Email] Found email: press@artist.com",
    ]


def test_instagram_email_extracts_obfuscated_email_from_existing_dom_attribute(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Attribute Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/attributeartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_fetch(session, url):
        return (
            "<html><body><div data-contact='contact (at) artist (dot) com'>Official profile</div></body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "contact@artist.com"
    assert seed_df.at[0, "Email_All"] == "contact@artist.com"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/attributeartist/",
        "[IG Email] Found email: contact@artist.com",
    ]


def test_instagram_email_one_hop_bio_link_recovers_direct_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "One Hop Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/onehopartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    bio_fetch_calls = []

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda session, url: (
            "<html><body><a href='https://linktr.ee/onehopartist'>Bio</a></body></html>",
            200,
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url="https://linktr.ee/onehopartist",
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: bookings@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert bio_fetch_calls == ["https://linktr.ee/onehopartist"]
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://linktr.ee/onehopartist"
    assert seed_df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "ig_enrich"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/onehopartist/",
        "[IG Email] Found email: bookings@artist.com",
    ]


def test_instagram_email_one_hop_bio_link_recovers_mailto(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Bio Mailto Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/biomailtoartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda session, url: (
            "<html><body><a href='https://beacons.ai/biomailtoartist'>Bio</a></body></html>",
            200,
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: cde.WebsiteFetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            html="<html><body><a href='mailto:hello@artist.com?subject=booking'>Email</a></body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "hello@artist.com"
    assert seed_df.at[0, "Email_All"] == "hello@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://beacons.ai/biomailtoartist"
    assert seed_df.at[0, "Email_Extract_Method"] == "mailto"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/biomailtoartist/",
        "[IG Email] Found email: hello@artist.com",
    ]


def test_instagram_email_invalid_bio_link_skips_one_hop_fetch(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Invalid Bio Link",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/invalidbiolink/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda session, url: ("<html><body><a href='javascript:void(0)'>Bio</a></body></html>", 200),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop fetch should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/invalidbiolink/",
        "[IG Email] no_email_visible",
    ]


def test_instagram_email_one_hop_preserves_multiple_emails_in_aggregate_output(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Bio Multi Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/biomultiartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda session, url: (
            "<html><body><a href='https://solo.to/biomultiartist'>Bio</a></body></html>",
            200,
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: cde.WebsiteFetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: first@artist.com Management: second@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "first@artist.com"
    assert seed_df.at[0, "Email_All"] == "first@artist.com;second@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://solo.to/biomultiartist"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/biomultiartist/",
        "[IG Email] Found email: first@artist.com",
    ]


def test_instagram_email_existing_row_email_skips_one_hop_stage(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Existing Email Artist",
            "Email": "existing@artist.com",
            "Email_All": "existing@artist.com",
            "Instagram_URL": "https://instagram.com/existingemailartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("instagram fetch should not run")),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop fetch should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.at[0, "Email"] == "existing@artist.com"
    assert seed_df.at[0, "Email_All"] == "existing@artist.com"
    assert logs == []


def test_instagram_email_one_hop_fetches_only_first_eligible_bio_link(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Single Hop Only",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/singlehoponly/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    bio_fetch_calls = []

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda session, url: (
            "<html><body>"
            "<a href='https://linktr.ee/firsttarget'>First</a>"
            "<a href='https://beacons.ai/secondtarget'>Second</a>"
            "</body></html>",
            200,
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            html="<html><body>bookings@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert bio_fetch_calls == ["https://linktr.ee/firsttarget"]
    assert seed_df.at[0, "Email"] == "bookings@artist.com"


def test_instagram_email_one_hop_does_not_follow_links_found_on_fetched_page(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No Second Hop",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/nosecondhop/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    bio_fetch_calls = []

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda session, url: (
            "<html><body><a href='https://campsite.bio/nosecondhop'>Bio</a></body></html>",
            200,
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            html="<html><body><a href='https://artist.com/contact'>Contact</a></body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert bio_fetch_calls == ["https://campsite.bio/nosecondhop"]
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/nosecondhop/",
        "[IG Email] no_email_visible",
    ]


def test_instagram_email_extracts_spaced_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Spaced Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/spacedartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_fetch(session, url):
        return ("<html><body>Bookings: hello @ artist . com</body></html>", 200)

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "hello@artist.com"
    assert seed_df.at[0, "Email_All"] == "hello@artist.com"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/spacedartist/",
        "[IG Email] Found email: hello@artist.com",
    ]


def test_instagram_email_extracts_direct_mailto(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Mailto Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/mailtoartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_fetch(session, url):
        return (
            "<html><body><a href='mailto:hello@artist.com?subject=booking'>Email</a></body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "hello@artist.com"
    assert seed_df.at[0, "Email_All"] == "hello@artist.com"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/mailtoartist/",
        "[IG Email] Found email: hello@artist.com",
    ]


def test_instagram_email_preserves_multiple_emails_in_aggregate_output(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Multi Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/multiartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_fetch(session, url):
        return (
            "<html><body>Bookings: first@artist.com Management: second@artist.com</body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "first@artist.com"
    assert seed_df.at[0, "Email_All"] == "first@artist.com;second@artist.com"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/multiartist/",
        "[IG Email] Found email: first@artist.com",
    ]


def test_instagram_hidden_contact_one_action_recovers_visible_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Hidden Contact Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/hiddencontactartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Provenance_JSON": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Profile with no visible email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>Email</button></body></html>",
            candidates=[{"text": "Email"}],
            click_effects={
                '[data-ig-hidden-contact="ig-hidden-contact-0"]': (
                    "<html><body><div role='dialog'>Bookings: bookings@artist.com</div></body></html>"
                )
            },
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-0"]']
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.instagram.com/hiddencontactartist/"
    assert seed_df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "ig_enrich"
    assert "instagram_hidden_contact_one_action" in seed_df.at[0, "Email_Provenance_JSON"]
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/hiddencontactartist/",
        "[IG Email] Found email: bookings@artist.com",
    ]


def test_instagram_hidden_contact_one_action_recovers_mailto(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Hidden Mailto Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/hiddenmailtoartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Provenance_JSON": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Profile with no visible email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>Email</button></body></html>",
            candidates=[{"text": "Email"}],
            click_effects={
                '[data-ig-hidden-contact="ig-hidden-contact-0"]': (
                    "<html><body><a href='mailto:hello@artist.com?subject=booking'>Email</a></body></html>"
                )
            },
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-0"]']
    assert seed_df.at[0, "Email"] == "hello@artist.com"
    assert seed_df.at[0, "Email_All"] == "hello@artist.com"
    assert seed_df.at[0, "Email_Extract_Method"] == "mailto"
    assert "instagram_hidden_contact_one_action" in seed_df.at[0, "Email_Provenance_JSON"]


def test_instagram_hidden_contact_one_action_skips_when_no_eligible_cta(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No CTA Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/noctaartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>No email on profile</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>Website</button><button>Subscribe</button></body></html>",
            candidates=[{"text": "Website"}, {"text": "Subscribe"}],
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/noctaartist/",
        "[IG Email] no_email_visible",
    ]


def test_instagram_hidden_contact_one_action_clicks_only_clear_contact_cta(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Single CTA Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/singlectaartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>No email yet</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>Website</button><button>Subscribe</button><button>Email</button></body></html>",
            candidates=[{"text": "Website"}, {"text": "Subscribe"}, {"text": "Email"}],
            click_effects={
                '[data-ig-hidden-contact="ig-hidden-contact-2"]': (
                    "<html><body><div>press@artist.com</div></body></html>"
                )
            },
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-2"]']
    assert seed_df.at[0, "Email"] == "press@artist.com"


def test_instagram_hidden_contact_one_action_prefers_highest_priority_candidate(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Priority Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/priorityartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>No email yet</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>Contact</button><button>Show email</button></body></html>",
            candidates=[{"text": "Contact"}, {"text": "Show email"}],
            click_effects={
                '[data-ig-hidden-contact="ig-hidden-contact-1"]': (
                    "<html><body><div>hello@artist.com</div></body></html>"
                )
            },
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-1"]']
    assert seed_df.at[0, "Email"] == "hello@artist.com"


def test_instagram_hidden_contact_one_action_stops_after_empty_reveal(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No Reveal Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/norevealartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>No email yet</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>Email</button><button>Contact</button></body></html>",
            candidates=[{"text": "Email"}, {"text": "Contact"}],
            click_effects={
                '[data-ig-hidden-contact="ig-hidden-contact-0"]': (
                    "<html><body><div role='dialog'>No email here</div></body></html>"
                )
            },
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-0"]']
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/norevealartist/",
        "[IG Email] no_email_visible",
    ]


def test_instagram_hidden_contact_one_action_budget_is_one_attempt_per_row(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Budget Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/budgetartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>No email yet</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>Email</button></body></html>",
            candidates=[{"text": "Email"}],
            click_effects={
                '[data-ig-hidden-contact="ig-hidden-contact-0"]': (
                    "<html><body><div>No email revealed</div></body></html>"
                )
            },
        ),
    )

    first_matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)
    second_matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert first_matched is False
    assert second_matched is False
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-0"]']


def test_instagram_hidden_contact_one_action_skips_ambiguous_cta(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Ambiguous Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/ambiguousartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>No email yet</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>More</button></body></html>",
            candidates=[{"text": "More"}],
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []


def test_instagram_hidden_contact_one_action_reads_overlay_without_second_click(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Overlay Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/overlayartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>No email yet</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>Email</button></body></html>",
            candidates=[{"text": "Email"}],
            click_effects={
                '[data-ig-hidden-contact="ig-hidden-contact-0"]': (
                    "<html><body><div role='dialog'><button>Copy</button><div>stage@artist.com</div></div></body></html>"
                )
            },
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-0"]']
    assert seed_df.at[0, "Email"] == "stage@artist.com"
