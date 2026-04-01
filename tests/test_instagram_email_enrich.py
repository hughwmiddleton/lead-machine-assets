from contextlib import contextmanager
import pandas as pd
import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde
from email_provenance import EMAIL_PROVENANCE_JSON_COL

_REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE = cde._open_instagram_live_page_bridge


def _make_worker(logs):
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    worker._set_platform_state = lambda *args, **kwargs: None
    return worker


def _seed_df(row):
    return pd.DataFrame([row], dtype=str).fillna("")


def _assert_log_contains(logs, expected):
    assert expected in logs


def _assert_no_log_startswith(logs, prefix):
    assert all(not line.startswith(prefix) for line in logs)


def _assert_ig_visit_and_outcome(logs, visit_url, outcome):
    assert logs[0] == f"[IG Email] Visiting {visit_url}"
    assert logs[-1] == outcome


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
    assert callable(fallback_kwargs["browser_ready_wait"])
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


def test_open_instagram_live_page_bridge_waits_for_render_before_return(monkeypatch):
    events = []

    class DummyPage(_DummyClosable):
        def goto(self, url, wait_until=None, timeout=None):  # noqa: ANN001
            events.append(("goto", url, wait_until, timeout))

        def content(self):
            return "<html><body><main>Rendered profile</main></body></html>"

    class DummyContext(_DummyClosable):
        def __init__(self, page):
            super().__init__()
            self._page = page

        def new_page(self):
            events.append(("new_page",))
            return self._page

    class DummyBrowser(_DummyClosable):
        def __init__(self, context):
            super().__init__()
            self._context = context

        def new_context(self):
            events.append(("new_context",))
            return self._context

    class DummyChromium:
        def __init__(self, browser):
            self._browser = browser

        def launch(self, headless=True):  # noqa: ANN001
            events.append(("launch", headless))
            return self._browser

    class DummyPlaywright(_DummyClosable):
        def __init__(self, browser):
            super().__init__()
            self.chromium = DummyChromium(browser)

        def stop(self):
            self.closed = True
            events.append(("stop",))

    class DummySyncPlaywrightRunner:
        def __init__(self, playwright):
            self._playwright = playwright

        def start(self):
            events.append(("start",))
            return self._playwright

    page = DummyPage()
    context = DummyContext(page)
    browser = DummyBrowser(context)
    playwright = DummyPlaywright(browser)

    monkeypatch.setattr(
        cde,
        "_load_instagram_playwright",
        lambda: (lambda: DummySyncPlaywrightRunner(playwright)),
    )

    def fake_wait_for_instagram_profile_render(page_arg, timeout_s):  # noqa: ANN001
        events.append(("wait", page_arg, timeout_s))

    monkeypatch.setattr(cde, "_wait_for_instagram_profile_render", fake_wait_for_instagram_profile_render)

    bridge = _REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE(
        "https://www.instagram.com/igartist/",
        timeout_s=12.5,
    )

    assert bridge is not None
    assert bridge.page is page
    assert bridge.snapshot_html() == "<html><body><main>Rendered profile</main></body></html>"
    assert events == [
        ("start",),
        ("launch", True),
        ("new_context",),
        ("new_page",),
        ("goto", "https://www.instagram.com/igartist/", "domcontentloaded", 12500.0),
        ("wait", page, 12.5),
    ]

    bridge.close()

    assert bridge.closed is True
    assert bridge.page.closed is True
    assert bridge.context.closed is True
    assert bridge.browser.closed is True
    assert bridge.playwright.closed is True
    assert events[-1] == ("stop",)


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/noemailhere/",
        "[IG Email] no_email_visible",
    )


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/igartist/",
        "[IG Email] no_email_visible",
    )


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/noemailhere/",
        "[IG Email] no_email_visible",
    )


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


def test_collect_instagram_bio_link_fetch_urls_preserves_anchor_priority_before_structured_surfaces():
    html = (
        "<html><head>"
        "<meta name='website' content='https://metaartist.com'>"
        "</head><body>"
        "<a href='https://artist.com/contact'>Contact</a>"
        "<script type='application/json'>"
        '{"profile":{"bio_links":[{"url":"https://linktr.ee/artist"}]}}'
        "</script>"
        "</body></html>"
    )

    urls = cde._collect_instagram_bio_link_fetch_urls(
        html,
        profile_url="https://www.instagram.com/artist/",
    )

    assert urls == [
        "https://artist.com/contact",
        "https://metaartist.com",
        "https://linktr.ee/artist",
    ]


def test_collect_instagram_bio_link_fetch_urls_extracts_structured_script_url_without_anchor():
    html = (
        "<html><body>"
        "<script type='application/json'>"
        '{"profile":{"bio_links":[{"url":"https://beacons.ai/scriptartist"}],'
        '"profile_pic_url":"https://cdn.instagram.com/avatar.jpg"}}'
        "</script>"
        "</body></html>"
    )

    urls = cde._collect_instagram_bio_link_fetch_urls(
        html,
        profile_url="https://www.instagram.com/scriptartist/",
    )

    assert urls == ["https://beacons.ai/scriptartist"]


def test_collect_instagram_bio_link_fetch_urls_extracts_meta_url_surface():
    html = (
        "<html><head>"
        "<meta property='og:url' content='https://www.instagram.com/metaartist/'>"
        "<meta name='website' content='https://metaartist.com'>"
        "<meta property='og:image' content='https://cdn.instagram.com/metaartist.jpg'>"
        "</head><body><div>Profile</div></body></html>"
    )

    urls = cde._collect_instagram_bio_link_fetch_urls(
        html,
        profile_url="https://www.instagram.com/metaartist/",
    )

    assert urls == ["https://metaartist.com"]


def test_collect_instagram_bio_link_fetch_urls_rejects_partial_and_non_http_structured_values():
    html = (
        "<html><head>"
        "<meta name='website' content='example.com'>"
        "</head><body>"
        "<script type='application/json'>"
        '{"profile":{"bio_links":[{"url":"/contact"},{"url":"instagram://user?username=artist"}],'
        '"website":"Visit https://artist.com"}}'
        "</script>"
        "</body></html>"
    )

    urls = cde._collect_instagram_bio_link_fetch_urls(
        html,
        profile_url="https://www.instagram.com/artist/",
    )

    assert urls == []


def test_collect_instagram_bio_link_fetch_urls_filters_instagram_self_urls_from_structured_surfaces():
    html = (
        "<html><head>"
        "<meta property='og:url' content='https://www.instagram.com/selfartist/'>"
        "</head><body>"
        "<script type='application/json'>"
        '{"profile":{"bio_links":[{"url":"https://instagr.am/selfartist/"}],'
        '"website":"https://instagram.com/selfartist"}}'
        "</script>"
        "</body></html>"
    )

    urls = cde._collect_instagram_bio_link_fetch_urls(
        html,
        profile_url="https://www.instagram.com/selfartist/",
    )

    assert urls == []


def test_collect_instagram_bio_link_fetch_urls_logs_empty_helper_summary():
    logs = []

    urls = cde._collect_instagram_bio_link_fetch_urls(
        "",
        profile_url="https://www.instagram.com/emptyartist/",
        log=logs.append,
    )

    assert urls == []
    assert logs == [
        "[IG OneHop] helper_summary anchors(raw=0 kept=0 dropped=0 drop_reasons=- sample=-) "
        "attributes(raw=0 kept=0 dropped=0 drop_reasons=- sample=-) "
        "meta(raw=0 kept=0 dropped=0 drop_reasons=- sample=-) "
        "structured_scripts(raw=0 kept=0 dropped=0 drop_reasons=- sample=-) "
        "total_unique=0 final_sample=-"
    ]


def test_collect_instagram_bio_link_fetch_urls_logs_survivors_and_drop_reasons():
    logs = []
    html = (
        "<html><head>"
        "<meta name='website' content='https://www.instagram.com/logartist/'>"
        "</head><body>"
        "<a href='javascript:void(0)'>Bad</a>"
        "<a href='https://artist.com/contact'>Contact</a>"
        "<script type='application/json'>"
        '{"profile":{"bio_links":[{"url":"https://artist.com/contact"},{"url":"https://beacons.ai/logartist"}]}}'
        "</script>"
        "</body></html>"
    )

    urls = cde._collect_instagram_bio_link_fetch_urls(
        html,
        profile_url="https://www.instagram.com/logartist/",
        log=logs.append,
    )

    assert urls == [
        "https://artist.com/contact",
        "https://beacons.ai/logartist",
    ]
    assert logs == [
        "[IG OneHop] helper_summary anchors(raw=2 kept=1 dropped=1 drop_reasons=non_http:1 sample=https://artist.com/contact) "
        "attributes(raw=1 kept=0 dropped=1 drop_reasons=self_instagram:1 sample=https://www.instagram.com/logartist/) "
        "meta(raw=1 kept=0 dropped=1 drop_reasons=self_instagram:1 sample=https://www.instagram.com/logartist/) "
        "structured_scripts(raw=2 kept=1 dropped=1 drop_reasons=duplicate:1 sample=https://beacons.ai/logartist) "
        "total_unique=2 final_sample=https://artist.com/contact,https://beacons.ai/logartist"
    ]


def test_select_instagram_onehop_target_prefers_link_hub_over_meta_help_and_cdn():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://help.instagram.com/12345",
            "https://static.cdninstagram.com/rsrc.php/v4/abc.css",
            "https://linktr.ee/artist",
        ],
        log=logs.append,
    )

    assert target == "https://linktr.ee/artist"
    _assert_log_contains(logs, "[IG OneHop] target_blocked reason=internal_meta url=https://help.instagram.com/12345")
    _assert_log_contains(logs, "[IG OneHop] target_blocked reason=static_asset url=https://static.cdninstagram.com/rsrc.php/v4/abc.css")
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=linkhub url=https://linktr.ee/artist")


def test_select_instagram_onehop_target_prefers_external_domain_over_internal_meta_links():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://about.meta.com/",
            "https://www.facebook.com/privacy/policy",
            "https://artist-example.com/contact",
        ],
        log=logs.append,
    )

    assert target == "https://artist-example.com/contact"
    _assert_log_contains(logs, "[IG OneHop] target_blocked reason=internal_meta url=https://about.meta.com/")
    _assert_log_contains(logs, "[IG OneHop] target_blocked reason=internal_meta url=https://www.facebook.com/privacy/policy")
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_domain url=https://artist-example.com/contact")


def test_select_instagram_onehop_target_returns_empty_when_only_blocked_candidates():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://help.instagram.com/999",
            "https://about.meta.com/",
            "https://cdninstagram.com/assets/app.js",
        ],
        log=logs.append,
    )

    assert target == ""
    _assert_log_contains(logs, "[IG OneHop] no_useful_target_after_ranking")


def test_instagram_email_one_hop_bio_link_recovers_direct_email_from_structured_script(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Structured Script Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/structuredscriptartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    bio_fetch_calls = []

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda session, url: (
            "<html><body>"
            "<script type='application/json'>"
            '{"profile":{"bio_links":[{"url":"https://beacons.ai/structuredscriptartist"}]}}'
            "</script>"
            "</body></html>",
            200,
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url="https://beacons.ai/structuredscriptartist",
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: structured@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert bio_fetch_calls == ["https://beacons.ai/structuredscriptartist"]
    assert seed_df.at[0, "Email"] == "structured@artist.com"
    assert seed_df.at[0, "Email_All"] == "structured@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://beacons.ai/structuredscriptartist"
    assert "instagram_bio_link_one_hop" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/structuredscriptartist/",
        "[IG Email] Found email: structured@artist.com",
    )
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=non_empty count=1 sample=https://beacons.ai/structuredscriptartist")
    _assert_log_contains(logs, "[IG OneHop] onehop_selected_target=https://beacons.ai/structuredscriptartist")
    _assert_log_contains(logs, "[IG OneHop] onehop_fetch_attempted=https://beacons.ai/structuredscriptartist")


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
            EMAIL_PROVENANCE_JSON_COL: "",
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
    assert "instagram_bio_link_one_hop" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/onehopartist/",
        "[IG Email] Found email: bookings@artist.com",
    )
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=non_empty count=1 sample=https://linktr.ee/onehopartist")
    _assert_log_contains(logs, "[IG OneHop] onehop_selected_target=https://linktr.ee/onehopartist")
    _assert_log_contains(logs, "[IG OneHop] onehop_fetch_attempted=https://linktr.ee/onehopartist")


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/biomailtoartist/",
        "[IG Email] Found email: hello@artist.com",
    )


def test_instagram_email_one_hop_bio_link_recovers_direct_email_from_rendered_live_surface(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered Link Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/renderedlinkartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    bio_fetch_calls = []
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Requests HTML without outbound bio link</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><a href='https://linktr.ee/renderedlinkartist'>Bio</a><button>Email</button></body></html>",
            candidates=[{"text": "Email"}],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url="https://linktr.ee/renderedlinkartist",
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: rendered@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert bio_fetch_calls == ["https://linktr.ee/renderedlinkartist"]
    assert seed_df.at[0, "Email"] == "rendered@artist.com"
    assert seed_df.at[0, "Email_All"] == "rendered@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://linktr.ee/renderedlinkartist"
    assert "instagram_bio_link_one_hop" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(
        logs,
        "[IG OneHop] live_surface_bio_link_urls state=non_empty count=1 sample=https://linktr.ee/renderedlinkartist",
    )


def test_instagram_hidden_contact_one_action_runs_after_live_surface_onehop_falls_through(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Shared Surface Hidden Contact Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/sharedsurfaceartist/",
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
        static_html="<html><body><div>No outbound bio link in requests HTML</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><button>Email</button></body></html>",
            candidates=[{"text": "Email"}],
            click_effects={
                '[data-ig-hidden-contact="ig-hidden-contact-0"]': (
                    "<html><body><div role='dialog'>Bookings: shared@artist.com</div></body></html>"
                )
            },
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-0"]']
    assert seed_df.at[0, "Email"] == "shared@artist.com"
    assert "instagram_hidden_contact_one_action" in seed_df.at[0, "Email_Provenance_JSON"]
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(logs, "[IG OneHop] live_surface_bio_link_urls state=empty count=0 sample=-")


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/invalidbiolink/",
        "[IG Email] no_email_visible",
    )
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls_empty")
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=")


def test_instagram_email_without_outbound_target_skips_one_hop_fetch(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "No Outbound Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/nooutboundartist/",
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
        lambda session, url: ("<html><body><div>No outbound bio link here</div></body></html>", 200),
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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/nooutboundartist/",
        "[IG Email] no_email_visible",
    )
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls_empty")
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=")


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/biomultiartist/",
        "[IG Email] Found email: first@artist.com",
    )


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


def test_instagram_email_one_hop_ranks_targets_but_still_fetches_only_one_url(monkeypatch):
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
            "<a href='https://help.instagram.com/111'>Meta Help</a>"
            "<a href='https://beacons.ai/secondtarget?utm_source=ig'>Second</a>"
            "<a href='https://linktr.ee/firsttarget'>First</a>"
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
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=linkhub url=https://linktr.ee/firsttarget")
    _assert_log_contains(logs, "[IG OneHop] target_blocked reason=internal_meta url=https://help.instagram.com/111")
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=https://beacons.ai/")


def test_instagram_email_one_hop_prefers_external_domain_over_internal_meta(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "External Domain First",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/externaldomainfirst/",
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
            "<a href='https://about.meta.com/'>Meta</a>"
            "<a href='https://artist-example.com/contact'>Artist Site</a>"
            "<a href='https://www.facebook.com/privacy/policy'>Privacy</a>"
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
            html="<html><body>team@artist-example.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert bio_fetch_calls == ["https://artist-example.com/contact"]
    assert seed_df.at[0, "Email"] == "team@artist-example.com"
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_domain url=https://artist-example.com/contact")
    _assert_log_contains(logs, "[IG OneHop] target_blocked reason=internal_meta url=https://about.meta.com")


def test_instagram_email_one_hop_blocked_only_targets_skip_fetch(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Blocked Targets Only",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/blockedtargets/",
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
            "<html><body>"
            "<a href='https://help.instagram.com/111'>Help</a>"
            "<a href='https://about.meta.com/'>Meta</a>"
            "<a href='https://static.cdninstagram.com/rsrc.php/v4/abc.css'>CSS</a>"
            "</body></html>",
            200,
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop fetch should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.at[0, "Email"] == ""
    _assert_log_contains(logs, "[IG OneHop] no_useful_target_after_ranking")
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=")


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/nosecondhop/",
        "[IG Email] no_email_visible",
    )


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/hiddencontactartist/",
        "[IG Email] Found email: bookings@artist.com",
    )


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/noctaartist/",
        "[IG Email] no_email_visible",
    )


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
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/norevealartist/",
        "[IG Email] no_email_visible",
    )


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
