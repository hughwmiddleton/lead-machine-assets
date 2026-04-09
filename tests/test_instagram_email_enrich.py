from contextlib import contextmanager
from bs4 import BeautifulSoup
import pandas as pd
import pytest
import re
from types import SimpleNamespace
import unicodedata

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


@pytest.fixture(autouse=True)
def _reset_html_fetcher_job_browsers():
    cde.html_fetcher._JOB_BROWSERS.clear()
    yield
    cde.html_fetcher._JOB_BROWSERS.clear()


class _DummyClosable:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _DummyInstagramHiddenContactPage(_DummyClosable):
    def __init__(
        self,
        html,
        *,
        rendered_text="",
        rendered_body_inner_text=None,
        rendered_body_text_content=None,
        rendered_main_text=None,
        rendered_document_text_content=None,
        rendered_aggregated_text=None,
        candidates=None,
        click_effects=None,
        runtime_structured_payloads=None,
        runtime_window_payloads=None,
        live_bio_link_control_values=None,
    ):
        super().__init__()
        self._html = html
        self._rendered_body_inner_text = (
            rendered_text if rendered_body_inner_text is None else rendered_body_inner_text
        )
        self._rendered_body_text_content = (
            rendered_text if rendered_body_text_content is None else rendered_body_text_content
        )
        self._rendered_main_text = rendered_text if rendered_main_text is None else rendered_main_text
        self._rendered_document_text_content = (
            rendered_text
            if rendered_document_text_content is None
            else rendered_document_text_content
        )
        self._rendered_aggregated_text = (
            rendered_text if rendered_aggregated_text is None else rendered_aggregated_text
        )
        self._candidates = list(candidates or [])
        self._click_effects = dict(click_effects or {})
        self._runtime_structured_payloads = list(runtime_structured_payloads or [])
        self._runtime_window_payloads = list(runtime_window_payloads or [])
        self._live_bio_link_control_values = (
            None if live_bio_link_control_values is None else list(live_bio_link_control_values)
        )
        self.click_calls = []
        self.wait_calls = []

    def content(self):
        return self._html

    def evaluate(self, script):  # noqa: ANN001
        script_text = str(script or "")
        if "ig-live-bio-link-control-surface" in script_text:
            if self._live_bio_link_control_values is not None:
                return list(self._live_bio_link_control_values)
            soup = BeautifulSoup(self._html, "html.parser")
            main = soup.select_one("main")
            if main is None:
                return []
            scope_roots = []
            header = main.select_one("header")
            if header is not None:
                scope_roots.append(header)
            for child in main.find_all(recursive=False):
                if len(scope_roots) >= cde._INSTAGRAM_LIVE_ONEHOP_MAX_SCOPE_ROOTS:
                    break
                tag_name = getattr(child, "name", "") or ""
                if child is header or tag_name in {"aside", "footer", "nav", "noscript", "script", "style"}:
                    continue
                scope_roots.append(child)
            if not scope_roots:
                scope_roots = [main]
            values = []
            seen = set()
            url_pattern = re.compile(r"https?://[^\s'\"<>()]+")

            def add_value(raw_value):
                value = str(raw_value or "").strip()
                if not value or value in seen:
                    return
                seen.add(value)
                values.append(value)

            def add_value_with_matches(raw_value):
                value = str(raw_value or "").strip()
                if not value:
                    return
                add_value(value)
                for match in url_pattern.findall(value):
                    add_value(match.rstrip(".,;:!?)]}"))

            def is_clickable(node):
                tag_name = getattr(node, "name", "") or ""
                role = str((node.attrs or {}).get("role", "")).strip().lower()
                return tag_name == "button" or (tag_name == "a" and node.get("href")) or role in {"button", "link"}

            def add_attr_values(node):
                attrs = getattr(node, "attrs", {}) or {}
                for attr_name, raw_value in attrs.items():
                    key = str(attr_name or "").strip().lower()
                    if not key or key == "style":
                        continue
                    if (
                        key == "href"
                        or key == "title"
                        or key == "aria-label"
                        or key == "onclick"
                        or key.startswith("data-")
                    ):
                        if isinstance(raw_value, (list, tuple, set)):
                            iterable = raw_value
                        else:
                            iterable = [raw_value]
                        for item in iterable:
                            add_value_with_matches(item)

            def add_anchor_values(node):
                if node is None or getattr(node, "name", "") != "a" or not node.get("href"):
                    return
                add_attr_values(node)
                add_value_with_matches(node.get("href"))

            def is_within_scope(candidate, scope_root):
                current = candidate
                while current is not None:
                    if current is scope_root:
                        return True
                    current = getattr(current, "parent", None)
                return False

            def collect_nearby_nodes(node, scope_root):
                nearby_nodes = []
                nearby_seen = set()

                def enqueue(candidate):
                    if candidate is None:
                        return
                    candidate_id = id(candidate)
                    if candidate_id in nearby_seen or len(nearby_nodes) >= 10:
                        return
                    if not is_within_scope(candidate, scope_root):
                        return
                    nearby_seen.add(candidate_id)
                    nearby_nodes.append(candidate)

                enqueue(node)
                cursor = node
                for _ in range(3):
                    parent = getattr(cursor, "parent", None) if cursor is not None else None
                    enqueue(parent)
                    enqueue(cursor.find_previous_sibling() if cursor is not None else None)
                    enqueue(cursor.find_next_sibling() if cursor is not None else None)
                    if parent is not None:
                        enqueue(parent.find_previous_sibling())
                        enqueue(parent.find_next_sibling())
                    if cursor is scope_root:
                        break
                    cursor = parent
                    if cursor is None:
                        break

                return nearby_nodes

            for root in scope_roots:
                nodes = []
                if is_clickable(root):
                    nodes.append(root)
                nodes.extend(root.select(cde._INSTAGRAM_LIVE_ONEHOP_CLICKABLE_SELECTOR))
                for node in nodes:
                    if not is_clickable(node):
                        continue
                    add_attr_values(node)
                    current = node
                    while current is not None and is_within_scope(current, root):
                        if current is not node and getattr(current, "name", "") == "a" and current.get("href"):
                            add_anchor_values(current)
                            break
                        current = getattr(current, "parent", None)
                    for nearby in collect_nearby_nodes(node, root):
                        add_anchor_values(nearby)
                        for anchor in nearby.select("a[href]")[:4]:
                            add_anchor_values(anchor)
            return values
        if "document.body" in script_text and "innerText" in script_text:
            return self._rendered_body_inner_text
        if "document.body" in script_text and "textContent" in script_text:
            return self._rendered_body_text_content
        if "document.querySelector('main')" in script_text:
            return self._rendered_main_text
        if "document.documentElement" in script_text and "textContent" in script_text:
            return self._rendered_document_text_content
        if "querySelectorAll('h1, span, div')" in script_text:
            return self._rendered_aggregated_text
        if "ig-hidden-contact" not in script_text and (
            "web_profile_info" in script_text or "bio_links" in script_text
        ):
            payloads = list(self._runtime_structured_payloads)
            if any(token in script_text for token in ('_sharedData', '__additionalData', '__initialData')):
                payloads.extend(self._runtime_window_payloads)
            return payloads
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


def _instagram_render_ready_marker_from_html(
    script,
    html,
    *,
    rendered_body_text="",
    rendered_main_text="",
):
    script_text = str(script or "")
    assert "return 'meta[property=\"og:description\"]';" not in script_text
    assert "profile_surface" in script_text
    assert "structured_script" not in script_text

    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main")
    if main is None:
        return False

    profile_structure = main.select_one("header, section, article")
    profile_content = main.select_one("a[href], button, img, h1, h2, ul li")
    body_text = " ".join(str(rendered_body_text or "").split())
    main_text = " ".join(str(rendered_main_text or "").split())
    if (
        profile_structure is not None
        and profile_content is not None
        and len(main_text) >= 16
        and len(body_text) >= len(main_text)
    ):
        return "profile_surface"
    return False


def _instagram_profile_surface_candidate_marker_from_html(script, html):
    script_text = str(script or "")
    assert "profile_surface_candidate" in script_text

    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main")
    if main is None:
        return False

    profile_structure = main.select_one("header, section, article")
    profile_content = main.select_one("a[href], button, img, h1, h2, ul li")
    if profile_structure is not None or profile_content is not None:
        return "profile_surface_candidate"
    return False


class _DummyInstagramRenderWaitPage:
    def __init__(self, html, *, clock=None, rendered_body_text="", rendered_main_text=""):
        self._html = html
        self._clock = clock
        self._rendered_body_text = rendered_body_text
        self._rendered_main_text = rendered_main_text
        self.evaluate_calls = []
        self.wait_calls = []

    def evaluate(self, script):  # noqa: ANN001
        self.evaluate_calls.append(str(script or ""))
        return _instagram_render_ready_marker_from_html(
            script,
            self._html,
            rendered_body_text=self._rendered_body_text,
            rendered_main_text=self._rendered_main_text,
        )

    def wait_for_timeout(self, timeout_ms):  # noqa: ANN001
        self.wait_calls.append(timeout_ms)
        if self._clock is not None:
            self._clock.advance(timeout_ms / 1000.0)


class _DummyInstagramRuntimePayloadWaitPage:
    def __init__(self, payload_states, *, clock=None):
        self._payload_states = list(payload_states)
        self._clock = clock
        self.evaluate_calls = []
        self.wait_calls = []

    def evaluate(self, script):  # noqa: ANN001
        self.evaluate_calls.append(str(script or ""))
        if self._payload_states:
            return self._payload_states[0]
        return []

    def wait_for_timeout(self, timeout_ms):  # noqa: ANN001
        self.wait_calls.append(timeout_ms)
        if len(self._payload_states) > 1:
            self._payload_states.pop(0)
        if self._clock is not None:
            self._clock.advance(timeout_ms / 1000.0)


class _FakeMonotonicClock:
    def __init__(self, start=1000.0):
        self.current = start

    def __call__(self):
        return self.current

    def advance(self, delta_s):
        self.current += delta_s


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


def _install_real_instagram_live_bridge(
    monkeypatch,
    *,
    landed_url,
    html,
    landed_title="",
    rendered_body_text="",
):
    class DummyPage(_DummyClosable):
        def __init__(self):
            super().__init__()
            self.url = ""
            self.evaluate_calls = []

        def goto(self, url, wait_until=None, timeout=None):  # noqa: ANN001
            self.url = landed_url

        def title(self):
            return landed_title

        def evaluate(self, script):  # noqa: ANN001
            script_text = str(script or "")
            self.evaluate_calls.append(script_text)
            if "document.body" in script_text and "innerText" in script_text:
                return rendered_body_text
            return False

        def content(self):
            return html

    class DummyContext(_DummyClosable):
        def __init__(self, page):
            super().__init__()
            self._page = page

        def new_page(self):
            return self._page

    class DummyBrowser(_DummyClosable):
        def __init__(self, context):
            super().__init__()
            self._context = context

        def new_context(self):
            return self._context

    class DummyChromium:
        def __init__(self, browser):
            self._browser = browser

        def launch(self, headless=True):  # noqa: ANN001
            return self._browser

    class DummyPlaywright(_DummyClosable):
        def __init__(self, browser):
            super().__init__()
            self.chromium = DummyChromium(browser)

        def stop(self):
            self.closed = True

    class DummySyncPlaywrightRunner:
        def __init__(self, playwright):
            self._playwright = playwright

        def start(self):
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
    monkeypatch.setattr(
        cde,
        "_wait_for_instagram_profile_render",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("wait should not run")),
    )
    monkeypatch.setattr(cde, "_open_instagram_live_page_bridge", _REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE)
    return page


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
        return True

    monkeypatch.setattr(cde, "_wait_for_instagram_profile_render", fake_wait_for_instagram_profile_render)

    bridge = _REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE(
        "https://www.instagram.com/igartist/",
        timeout_s=12.5,
    )

    assert bridge is not None
    assert bridge.owns_browser_stack is True
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


def test_open_instagram_live_page_bridge_reuses_cached_html_fetcher_context(monkeypatch):
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

    shared_playwright = _DummyClosable()
    shared_browser = _DummyClosable()
    shared_context = DummyContext(DummyPage())
    cde.html_fetcher._JOB_BROWSERS["global"] = SimpleNamespace(
        playwright=shared_playwright,
        browser=shared_browser,
        context=shared_context,
    )

    monkeypatch.setattr(
        cde,
        "_load_instagram_playwright",
        lambda: (_ for _ in ()).throw(AssertionError("sync_playwright should not start")),
    )

    def fake_wait_for_instagram_profile_render(page_arg, timeout_s):  # noqa: ANN001
        events.append(("wait", page_arg, timeout_s))
        return True

    monkeypatch.setattr(cde, "_wait_for_instagram_profile_render", fake_wait_for_instagram_profile_render)

    bridge = _REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE(
        "https://www.instagram.com/igartist/",
        timeout_s=12.5,
    )

    assert bridge is not None
    assert bridge.owns_browser_stack is False
    assert bridge.playwright is shared_playwright
    assert bridge.browser is shared_browser
    assert bridge.context is shared_context
    assert events == [
        ("new_page",),
        ("goto", "https://www.instagram.com/igartist/", "domcontentloaded", 12500.0),
        ("wait", bridge.page, 12.5),
    ]

    bridge.close()

    assert bridge.page.closed is True
    assert shared_context.closed is False
    assert shared_browser.closed is False
    assert shared_playwright.closed is False


def test_open_instagram_live_page_bridge_shared_context_failure_only_closes_page(monkeypatch):
    page = _DummyClosable()

    def failing_goto(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("navigation failed")

    page.goto = failing_goto
    shared_context = _DummyClosable()
    shared_context.new_page = lambda: page
    shared_browser = _DummyClosable()
    shared_playwright = _DummyClosable()
    cde.html_fetcher._JOB_BROWSERS["global"] = SimpleNamespace(
        playwright=shared_playwright,
        browser=shared_browser,
        context=shared_context,
    )

    monkeypatch.setattr(
        cde,
        "_load_instagram_playwright",
        lambda: (_ for _ in ()).throw(AssertionError("sync_playwright should not start")),
    )

    bridge = _REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE("https://www.instagram.com/igartist/")

    assert bridge is None
    assert page.closed is True
    assert shared_context.closed is False
    assert shared_browser.closed is False
    assert shared_playwright.closed is False


def test_open_instagram_live_page_bridge_profile_shaped_url_non_profile_surface_fails_before_render_wait(
    monkeypatch,
):
    class DummyPage(_DummyClosable):
        def __init__(self):
            super().__init__()
            self.url = ""
            self.evaluate_calls = []

        def goto(self, url, wait_until=None, timeout=None):  # noqa: ANN001
            self.url = "https://www.instagram.com/villyszn/"

        def evaluate(self, script):  # noqa: ANN001
            self.evaluate_calls.append(str(script or ""))
            return False

    class DummyContext(_DummyClosable):
        def __init__(self, page):
            super().__init__()
            self._page = page

        def new_page(self):
            return self._page

    class DummyBrowser(_DummyClosable):
        def __init__(self, context):
            super().__init__()
            self._context = context

        def new_context(self):
            return self._context

    class DummyChromium:
        def __init__(self, browser):
            self._browser = browser

        def launch(self, headless=True):  # noqa: ANN001
            return self._browser

    class DummyPlaywright(_DummyClosable):
        def __init__(self, browser):
            super().__init__()
            self.chromium = DummyChromium(browser)

        def stop(self):
            self.closed = True

    class DummySyncPlaywrightRunner:
        def __init__(self, playwright):
            self._playwright = playwright

        def start(self):
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
    monkeypatch.setattr(
        cde,
        "_wait_for_instagram_profile_render",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("wait should not run")),
    )

    bridge = _REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE(
        "https://www.instagram.com/igartist/",
        timeout_s=12.5,
    )

    assert bridge is None
    assert cde._INSTAGRAM_PROFILE_SURFACE_CANDIDATE_JS in page.evaluate_calls
    assert page.closed is True
    assert context.closed is True
    assert browser.closed is True
    assert playwright.closed is True


def test_open_instagram_live_page_bridge_logged_out_html_handoff_is_rejected(monkeypatch):
    events = []
    logged_out_html = """
    <html>
      <head>
        <title>shellartist (@shellartist) • Instagram photos and videos</title>
        <meta property="og:description" content="1,234 Followers, 56 Following, 12 Posts - See Instagram photos and videos from shellartist (@shellartist)" />
      </head>
      <body>
        <script type="application/ld+json">{"@type":"ProfilePage"}</script>
        <div id="mount">shellartist logged-out profile payload</div>
      </body>
    </html>
    """
    assert _instagram_profile_surface_candidate_marker_from_html(
        cde._INSTAGRAM_PROFILE_SURFACE_CANDIDATE_JS,
        logged_out_html,
    ) is False

    class DummyPage(_DummyClosable):
        def __init__(self):
            super().__init__()
            self.url = ""
            self.evaluate_calls = []

        def goto(self, url, wait_until=None, timeout=None):  # noqa: ANN001
            self.url = "https://www.instagram.com/shellartist/"
            events.append(("goto", self.url, wait_until, timeout))

        def title(self):
            return "shellartist (@shellartist) • Instagram photos and videos"

        def evaluate(self, script):  # noqa: ANN001
            script_text = str(script or "")
            self.evaluate_calls.append(script_text)
            if "document.body" in script_text and "innerText" in script_text:
                return "shellartist logged-out profile payload"
            return False

        def content(self):
            return logged_out_html

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
    monkeypatch.setattr(
        cde,
        "_wait_for_instagram_profile_render",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("wait should not run")),
    )

    bridge = _REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE(
        "https://www.instagram.com/shellartist/",
        timeout_s=12.5,
    )

    assert bridge is None
    assert events[:5] == [
        ("start",),
        ("launch", True),
        ("new_context",),
        ("new_page",),
        ("goto", "https://www.instagram.com/shellartist/", "domcontentloaded", 12500.0),
    ]
    assert cde._INSTAGRAM_PROFILE_SURFACE_CANDIDATE_JS in page.evaluate_calls
    assert events[-1] == ("stop",)
    assert page.closed is True
    assert context.closed is True
    assert browser.closed is True
    assert playwright.closed is True
    assert cde._instagram_landed_page_is_html_handoff_usable(
        "https://www.instagram.com/shellartist/",
        "https://www.instagram.com/shellartist/",
        logged_out_html,
    ) is False


def test_open_instagram_live_page_bridge_profile_shell_candidate_still_enters_render_wait(monkeypatch):
    events = []
    candidate_html = (
        "<html><body><main><header><h1>Shell Artist</h1><button>Email</button></header></main></body></html>"
    )
    assert (
        _instagram_render_ready_marker_from_html(
            cde._INSTAGRAM_RENDER_READY_JS,
            candidate_html,
            rendered_body_text="Shell Artist",
            rendered_main_text="Shell Artist",
        )
        is False
    )

    class DummyPage(_DummyClosable):
        def __init__(self):
            super().__init__()
            self.url = ""

        def goto(self, url, wait_until=None, timeout=None):  # noqa: ANN001
            self.url = "https://www.instagram.com/accounts/login/"
            events.append(("goto", self.url, wait_until, timeout))

        def evaluate(self, script):  # noqa: ANN001
            events.append(("evaluate", str(script or "")))
            return _instagram_profile_surface_candidate_marker_from_html(script, candidate_html)

        def content(self):
            return candidate_html

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
        return True

    monkeypatch.setattr(cde, "_wait_for_instagram_profile_render", fake_wait_for_instagram_profile_render)

    bridge = _REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE(
        "https://www.instagram.com/igartist/",
        timeout_s=12.5,
    )

    assert bridge is not None
    assert bridge.page is page
    assert events[:5] == [
        ("start",),
        ("launch", True),
        ("new_context",),
        ("new_page",),
        ("goto", "https://www.instagram.com/accounts/login/", "domcontentloaded", 12500.0),
    ]
    assert ("evaluate", cde._INSTAGRAM_PROFILE_SURFACE_CANDIDATE_JS) in events
    assert events[-1] == ("wait", page, 12.5)

    bridge.close()


def test_open_instagram_live_page_bridge_profile_shell_candidate_that_never_renders_is_rejected(
    monkeypatch,
):
    events = []
    candidate_html = (
        "<html><body><main><header><h1>Shell Artist</h1><button>Email</button></header></main></body></html>"
    )

    class DummyPage(_DummyClosable):
        def __init__(self):
            super().__init__()
            self.url = ""

        def goto(self, url, wait_until=None, timeout=None):  # noqa: ANN001
            self.url = "https://www.instagram.com/accounts/login/"
            events.append(("goto", self.url, wait_until, timeout))

        def evaluate(self, script):  # noqa: ANN001
            events.append(("evaluate", str(script or "")))
            return _instagram_profile_surface_candidate_marker_from_html(script, candidate_html)

        def content(self):
            return candidate_html

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
        return False

    monkeypatch.setattr(cde, "_wait_for_instagram_profile_render", fake_wait_for_instagram_profile_render)

    bridge = _REAL_OPEN_INSTAGRAM_LIVE_PAGE_BRIDGE(
        "https://www.instagram.com/igartist/",
        timeout_s=12.5,
    )

    assert bridge is None
    assert events[:5] == [
        ("start",),
        ("launch", True),
        ("new_context",),
        ("new_page",),
        ("goto", "https://www.instagram.com/accounts/login/", "domcontentloaded", 12500.0),
    ]
    assert ("evaluate", cde._INSTAGRAM_PROFILE_SURFACE_CANDIDATE_JS) in events
    assert events[-2:] == [("wait", page, 12.5), ("stop",)]
    assert page.closed is True
    assert context.closed is True
    assert browser.closed is True
    assert playwright.closed is True


def test_wait_for_instagram_profile_render_meta_only_shell_is_not_ready(monkeypatch):
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(cde.time, "monotonic", clock)
    page = _DummyInstagramRenderWaitPage(
        "<html><head><meta property='og:description' content='Official profile'></head>"
        "<body><div>Shell only</div></body></html>",
        clock=clock,
    )

    render_ready = cde._wait_for_instagram_profile_render(page, timeout_s=0.1)

    assert render_ready is False
    assert len(page.evaluate_calls) == 1
    assert page.wait_calls == [100]


def test_wait_for_instagram_profile_render_structured_script_only_is_not_ready(monkeypatch):
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(cde.time, "monotonic", clock)
    page = _DummyInstagramRenderWaitPage(
        "<html><body>"
        "<script type='application/json'>"
        '{"profile":{"bio_links":[{"url":"https://linktr.ee/render-gate-artist"}]}}'
        "</script>"
        "<main><header><h1>Render Gate Artist</h1><button>Email</button></header></main>"
        "</body></html>",
        clock=clock,
    )

    render_ready = cde._wait_for_instagram_profile_render(page, timeout_s=0.1)

    assert render_ready is False
    assert len(page.evaluate_calls) == 1
    assert page.wait_calls == [100]


def test_wait_for_instagram_profile_render_accepts_rendered_profile_surface(monkeypatch):
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(cde.time, "monotonic", clock)
    page = _DummyInstagramRenderWaitPage(
        "<html><head><meta property='og:description' content='Official profile'></head>"
        "<body><main><header><h1>Rendered Link Artist</h1><button>Email</button></header>"
        "<section><a href='https://linktr.ee/renderedlinkartist'>Bio</a></section></main></body></html>",
        clock=clock,
        rendered_body_text="Rendered Link Artist Bio and booking details",
        rendered_main_text="Rendered Link Artist Bio and booking details",
    )

    render_ready = cde._wait_for_instagram_profile_render(page, timeout_s=0.25)

    assert render_ready is True
    assert len(page.evaluate_calls) == 1
    assert page.wait_calls == []


def test_wait_for_instagram_profile_render_polling_timeout_stays_bounded(monkeypatch):
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(cde.time, "monotonic", clock)
    page = _DummyInstagramRenderWaitPage(
        "<html><head><meta property='og:description' content='Official profile'></head>"
        "<body><div>Still shell only</div></body></html>",
        clock=clock,
    )

    timeout_s = 10.0
    render_ready = cde._wait_for_instagram_profile_render(page, timeout_s=timeout_s)

    assert render_ready is False
    expected_timeout_ms = int(timeout_s * 1000)
    assert expected_timeout_ms - 1 <= sum(page.wait_calls) <= expected_timeout_ms
    assert max(page.wait_calls) == 100


def test_wait_for_instagram_runtime_bio_link_structured_payloads_thin_shell_times_out_cleanly(
    monkeypatch,
):
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(cde.time, "monotonic", clock)
    page = _DummyInstagramRuntimePayloadWaitPage([[]], clock=clock)

    payloads = cde._wait_for_instagram_runtime_bio_link_structured_payloads(page, timeout_s=0.1)

    assert payloads == []
    assert len(page.evaluate_calls) == 2
    assert page.wait_calls == [100]


def test_wait_for_instagram_runtime_bio_link_structured_payloads_accepts_hydrated_payload_state(
    monkeypatch,
):
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(cde.time, "monotonic", clock)
    payload = {"web_profile_info": {"bio_links": [{"url": "https://linktr.ee/runtimewaitartist"}]}}
    page = _DummyInstagramRuntimePayloadWaitPage([[], [payload]], clock=clock)

    payloads = cde._wait_for_instagram_runtime_bio_link_structured_payloads(page, timeout_s=0.25)

    assert payloads == [payload]
    assert len(page.evaluate_calls) == 2
    assert page.wait_calls == [100]


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


def test_instagram_email_stylized_meta_description_writes_email_without_fallback(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    stylized_email = "𝙇𝙖𝙘𝙚𝙙𝙪𝙥𝙈𝙂𝙈𝙏@𝙜𝙢𝙖𝙞𝙡.𝙘𝙤𝙢"
    seed_df = _seed_df(
        {
            "Artist Name": "Stylized Meta Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/stylizedmetaartist/?hl=en#bio",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    def fake_fetch(session, url):
        return (
            "<html><head>"
            f"<meta name='description' content='Bookings: {stylized_email}'>"
            "</head><body><div>Official profile</div></body></html>",
            200,
        )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", fake_fetch)
    monkeypatch.setattr(
        cde,
        "_instagram_onehop_emails_from_surface",
        lambda *args, **kwargs: pytest.fail("one-hop should not run"),
    )
    monkeypatch.setattr(
        cde,
        "_open_instagram_live_page_bridge",
        lambda *args, **kwargs: pytest.fail("live bridge should not run"),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "lacedupmgmt@gmail.com"
    assert seed_df.at[0, "Email_All"] == "lacedupmgmt@gmail.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.instagram.com/stylizedmetaartist/"
    assert seed_df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "ig_enrich"
    assert logs == [
        "[IG Email] Visiting https://www.instagram.com/stylizedmetaartist/",
        "[IG Email] Found email: lacedupmgmt@gmail.com",
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


def test_extract_instagram_direct_profile_candidate_emails_preserves_plain_ascii_meta_email():
    html = (
        "<html><head>"
        "<meta property='og:description' content='Bookings: bookings@artist.com'>"
        "</head><body><div>Official profile</div></body></html>"
    )

    emails = cde._extract_instagram_direct_profile_candidate_emails(html)

    assert emails == ["bookings@artist.com"]


def test_extract_instagram_direct_profile_candidate_emails_ignores_stylized_non_email_text():
    html = (
        "<html><head>"
        "<meta name='description' content='𝘽𝙤𝙤𝙠𝙞𝙣𝙜𝙨 𝙫𝙞𝙖 𝙜𝙢𝙖𝙞𝙡 𝙙𝙤𝙩 𝙘𝙤𝙢'>"
        "</head><body><div>Official profile</div></body></html>"
    )

    emails = cde._extract_instagram_direct_profile_candidate_emails(html)

    assert emails == []


def test_collect_instagram_bio_link_fetch_urls_preserves_html_priority_with_runtime_structured_payloads():
    html = (
        "<html><head>"
        "<meta name='website' content='https://metaartist.com'>"
        "</head><body>"
        "<a href='https://artist.com/contact'>Contact</a>"
        "</body></html>"
    )

    urls = cde._collect_instagram_bio_link_fetch_urls(
        html,
        profile_url="https://www.instagram.com/runtimeartist/",
        runtime_structured_payloads=[
            {"user": {"bio_links": [{"url": "https://beacons.ai/runtimeartist"}]}}
        ],
    )

    assert urls == [
        "https://artist.com/contact",
        "https://metaartist.com",
        "https://beacons.ai/runtimeartist",
    ]


def test_extract_instagram_onehop_profile_surface_html_excludes_head_and_footer_chrome():
    html = (
        "<html><head>"
        "<meta name='website' content='https://about.meta.com/'>"
        "</head><body>"
        "<nav><a href='https://help.instagram.com/12345'>Help</a></nav>"
        "<main><a href='https://linktr.ee/scopedartist'>Bio</a></main>"
        "<footer><a href='https://www.threads.com/@scopedartist'>Threads</a></footer>"
        "<script type='application/json'>"
        '{"profile":{"bio_links":[{"url":"https://beacons.ai/chrome-should-not-pass-through-html"}]}}'
        "</script>"
        "</body></html>"
    )

    scoped_html = cde._extract_instagram_onehop_profile_surface_html(html)

    assert "https://linktr.ee/scopedartist" in scoped_html
    assert "https://about.meta.com/" not in scoped_html
    assert "https://help.instagram.com/12345" not in scoped_html
    assert "https://www.threads.com/@scopedartist" not in scoped_html
    assert "https://beacons.ai/chrome-should-not-pass-through-html" not in scoped_html


def test_iter_instagram_bio_link_structured_values_preserves_urls_and_admits_direct_emails_only():
    payload = {
        "bio_links": [
            {
                "url": "https://beacons.ai/artist",
                "title": "Contact the team",
            }
        ],
        "website": "https://artist.com/contact",
        "business_email": "Bookings@Artist.com",
        "public_email": "press@artist.com",
        "bio": "hello@artist.com",
    }

    values = list(cde._iter_instagram_bio_link_structured_values(payload))

    assert "https://beacons.ai/artist" in values
    assert "https://artist.com/contact" in values
    assert "bookings@artist.com" in values
    assert "press@artist.com" in values
    assert "Contact the team" not in values
    assert "hello@artist.com" not in values


def test_collect_instagram_runtime_bio_link_structured_payloads_preserves_script_surface():
    page = _DummyInstagramHiddenContactPage(
        "<html><body></body></html>",
        runtime_structured_payloads=[
            {"bio_links": [{"url": "https://beacons.ai/script-runtime-artist"}]}
        ],
    )

    payloads = cde._collect_instagram_runtime_bio_link_structured_payloads(page)

    assert payloads == [
        {"bio_links": [{"url": "https://beacons.ai/script-runtime-artist"}]}
    ]


def test_collect_instagram_runtime_bio_link_structured_payloads_reads_window_runtime_surface():
    page = _DummyInstagramHiddenContactPage(
        "<html><body></body></html>",
        runtime_window_payloads=[
            {
                "web_profile_info": {
                    "bio_links": [{"url": "https://linktr.ee/runtimewindowartist"}]
                }
            }
        ],
    )

    payloads = cde._collect_instagram_runtime_bio_link_structured_payloads(page)

    assert payloads == [
        {
            "web_profile_info": {
                "bio_links": [{"url": "https://linktr.ee/runtimewindowartist"}]
            }
        }
    ]


def test_collect_instagram_live_profile_clickable_bio_link_urls_scopes_to_main_profile_controls():
    page = _DummyInstagramHiddenContactPage(
        "<html><body>"
        "<nav><button data-url='https://help.instagram.com/12345'>Help</button></nav>"
        "<main><header>"
        "<div role='link' data-url='https://linktr.ee/controlsurfaceartist'>Bio</div>"
        "</header></main>"
        "<footer><button data-url='https://www.threads.com/@controlsurfaceartist'>Threads</button></footer>"
        "</body></html>"
    )

    urls = cde._collect_instagram_live_profile_clickable_bio_link_urls(
        page,
        profile_url="https://www.instagram.com/controlsurfaceartist/",
    )

    assert urls == ["https://linktr.ee/controlsurfaceartist"]


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            "<html><body>"
            "<nav><a href='https://help.instagram.com/12345'>Help</a></nav>"
            "<main><header>"
            "<div role='button'><a href='https://linktr.ee/descendantcontrolartist'>Bio</a></div>"
            "</header></main>"
            "<footer><a href='https://www.threads.com/@descendantcontrolartist'>Threads</a></footer>"
            "</body></html>",
            "https://linktr.ee/descendantcontrolartist",
        ),
        (
            "<html><body>"
            "<nav><a href='https://help.instagram.com/12345'>Help</a></nav>"
            "<main><header>"
            "<a href='https://beacons.ai/ancestorcontrolartist'><div role='button'>Bio</div></a>"
            "</header></main>"
            "<footer><a href='https://www.threads.com/@ancestorcontrolartist'>Threads</a></footer>"
            "</body></html>",
            "https://beacons.ai/ancestorcontrolartist",
        ),
        (
            "<html><body>"
            "<nav><a href='https://help.instagram.com/12345'>Help</a></nav>"
            "<main><header>"
            "<div><button>Email</button><span><a href='https://linktr.ee/nearbycontrolartist'>Bio</a></span></div>"
            "</header></main>"
            "<footer><a href='https://www.threads.com/@nearbycontrolartist'>Threads</a></footer>"
            "</body></html>",
            "https://linktr.ee/nearbycontrolartist",
        ),
    ],
)
def test_collect_instagram_live_profile_clickable_bio_link_urls_recovers_dom_relative_anchor_surfaces(
    html,
    expected,
):
    page = _DummyInstagramHiddenContactPage(html)

    urls = cde._collect_instagram_live_profile_clickable_bio_link_urls(
        page,
        profile_url="https://www.instagram.com/domrelativeartist/",
    )

    assert urls == [expected]


def test_collect_instagram_live_profile_clickable_bio_link_urls_recovers_runtime_adjoining_value_when_clickable_attrs_are_empty():
    page = _DummyInstagramHiddenContactPage(
        "<html><body><main><header><button>Email</button></header></main></body></html>",
        live_bio_link_control_values=[
            '{"navigation":{"target":"https://beacons.ai/runtimeadjacentcontrolartist"}}'
        ],
    )

    urls = cde._collect_instagram_live_profile_clickable_bio_link_urls(
        page,
        profile_url="https://www.instagram.com/runtimeadjacentcontrolartist/",
    )

    assert urls == ["https://beacons.ai/runtimeadjacentcontrolartist"]


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


def test_select_instagram_onehop_target_demotes_utility_doc_page_below_real_outbound_link():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://developers.facebook.com/docs/instagram",
            "https://linktr.ee/artist",
        ],
        log=logs.append,
    )

    assert target == "https://linktr.ee/artist"
    winner_trace = (
        "[IG OneHop] ranked_candidate rank=1 tier=linkhub specificity=specific_path "
        "generic_root=0 low_value_platform=0 utility_info=0 url=https://linktr.ee/artist"
    )
    loser_trace = (
        "[IG OneHop] ranked_candidate rank=2 tier=external_info specificity=specific_path "
        "generic_root=0 low_value_platform=1 utility_info=1 url=https://developers.facebook.com/docs/instagram"
    )
    _assert_log_contains(logs, winner_trace)
    _assert_log_contains(logs, loser_trace)
    assert logs.index(winner_trace) < logs.index(loser_trace)
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=linkhub url=https://linktr.ee/artist")


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


def test_select_instagram_onehop_target_prefers_specific_target_over_generic_threads_root():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://www.threads.com",
            "https://artist-example.com/contact",
        ],
        log=logs.append,
    )

    assert target == "https://artist-example.com/contact"
    _assert_log_contains(logs, "[IG OneHop] ranked_candidates count=2")
    winner_trace = (
        "[IG OneHop] ranked_candidate rank=1 tier=external_domain specificity=specific_path "
        "generic_root=0 low_value_platform=0 utility_info=0 url=https://artist-example.com/contact"
    )
    loser_trace = (
        "[IG OneHop] ranked_candidate rank=2 tier=external_info specificity=generic_root "
        "generic_root=1 low_value_platform=1 utility_info=0 url=https://www.threads.com"
    )
    _assert_log_contains(logs, winner_trace)
    _assert_log_contains(logs, loser_trace)
    assert logs.index(winner_trace) < logs.index(loser_trace)
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_domain url=https://artist-example.com/contact")
    _assert_log_contains(
        logs,
        "[IG OneHop] ranked_target_decision specificity=specific_path generic_root_demotion=1 low_value_platform_demotion=1 fallback_weak=0 url=https://artist-example.com/contact",
    )


def test_select_instagram_onehop_target_prefers_specific_page_over_generic_root():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://artist-example.com/",
            "https://press.artist-example.com/contact",
        ],
        log=logs.append,
    )

    assert target == "https://press.artist-example.com/contact"
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_domain url=https://press.artist-example.com/contact")
    _assert_log_contains(
        logs,
        "[IG OneHop] ranked_target_decision specificity=specific_path generic_root_demotion=1 low_value_platform_demotion=0 fallback_weak=0 url=https://press.artist-example.com/contact",
    )


def test_select_instagram_onehop_target_retains_best_weak_fallback_when_doc_page_beats_generic_junk():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://developers.facebook.com/docs/instagram",
            "https://www.meta.ai/?utm_source=foa_web_footer",
            "https://www.threads.com",
        ],
        log=logs.append,
    )

    assert target == "https://developers.facebook.com/docs/instagram"
    _assert_log_contains(logs, "[IG OneHop] ranked_candidates count=3")
    winner_trace = (
        "[IG OneHop] ranked_candidate rank=1 tier=external_info specificity=specific_path "
        "generic_root=0 low_value_platform=1 utility_info=1 url=https://developers.facebook.com/docs/instagram"
    )
    _assert_log_contains(logs, winner_trace)
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_info url=https://developers.facebook.com/docs/instagram")


def test_select_instagram_onehop_target_retains_fallback_when_only_weak_candidates_exist():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://artist-example.com/",
            "https://www.threads.com",
        ],
        log=logs.append,
    )

    assert target == "https://artist-example.com/"
    _assert_log_contains(logs, "[IG OneHop] ranked_candidates count=2")
    winner_trace = (
        "[IG OneHop] ranked_candidate rank=1 tier=external_domain specificity=generic_root "
        "generic_root=1 low_value_platform=0 utility_info=0 url=https://artist-example.com/"
    )
    loser_trace = (
        "[IG OneHop] ranked_candidate rank=2 tier=external_info specificity=generic_root "
        "generic_root=1 low_value_platform=1 utility_info=0 url=https://www.threads.com"
    )
    _assert_log_contains(logs, winner_trace)
    _assert_log_contains(logs, loser_trace)
    assert logs.index(winner_trace) < logs.index(loser_trace)
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_domain url=https://artist-example.com/")
    _assert_log_contains(
        logs,
        "[IG OneHop] ranked_target_decision specificity=generic_root generic_root_demotion=0 low_value_platform_demotion=0 fallback_weak=1 url=https://artist-example.com/",
    )


def test_select_instagram_onehop_target_keeps_user_specific_threads_page_eligible():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://artist-example.com/",
            "https://www.threads.com/@artistname",
        ],
        log=logs.append,
    )

    assert target == "https://www.threads.com/@artistname"
    _assert_log_contains(logs, "[IG OneHop] ranked_candidates count=2")
    winner_trace = (
        "[IG OneHop] ranked_candidate rank=1 tier=external_info specificity=specific_path "
        "generic_root=0 low_value_platform=1 utility_info=0 url=https://www.threads.com/@artistname"
    )
    loser_trace = (
        "[IG OneHop] ranked_candidate rank=2 tier=external_domain specificity=generic_root "
        "generic_root=1 low_value_platform=0 utility_info=0 url=https://artist-example.com/"
    )
    _assert_log_contains(logs, winner_trace)
    _assert_log_contains(logs, loser_trace)
    assert logs.index(winner_trace) < logs.index(loser_trace)
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_info url=https://www.threads.com/@artistname")
    _assert_log_contains(
        logs,
        "[IG OneHop] ranked_target_decision specificity=specific_path generic_root_demotion=1 low_value_platform_demotion=0 fallback_weak=0 url=https://www.threads.com/@artistname",
    )


def test_select_instagram_onehop_target_keeps_user_specific_platform_page_above_utility_doc_page():
    logs = []

    target = cde._select_instagram_onehop_target(
        [
            "https://developers.facebook.com/docs/instagram",
            "https://www.threads.com/@artistname",
        ],
        log=logs.append,
    )

    assert target == "https://www.threads.com/@artistname"
    winner_trace = (
        "[IG OneHop] ranked_candidate rank=1 tier=external_info specificity=specific_path "
        "generic_root=0 low_value_platform=1 utility_info=0 url=https://www.threads.com/@artistname"
    )
    loser_trace = (
        "[IG OneHop] ranked_candidate rank=2 tier=external_info specificity=specific_path "
        "generic_root=0 low_value_platform=1 utility_info=1 url=https://developers.facebook.com/docs/instagram"
    )
    _assert_log_contains(logs, winner_trace)
    _assert_log_contains(logs, loser_trace)
    assert logs.index(winner_trace) < logs.index(loser_trace)
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_info url=https://www.threads.com/@artistname")


def test_instagram_onehop_emails_from_surface_skips_fetch_for_selected_weak_utility_target(monkeypatch):
    logs = []
    fetch_calls = []

    def fake_fetch_website_html_bounded(session, url, **kwargs):  # noqa: ANN001
        fetch_calls.append(url)
        pytest.fail("weak utility-only winner should not trigger one-hop fetch")

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch_website_html_bounded)

    emails, source_url, extract_method, onehop_target = cde._instagram_onehop_emails_from_surface(
        None,
        "<html><body>"
        "<a href='https://developers.facebook.com/docs/instagram'>Docs</a>"
        "<a href='https://www.meta.ai/?utm_source=foa_web_footer'>Meta AI</a>"
        "<a href='https://www.threads.com'>Threads</a>"
        "</body></html>",
        profile_url="https://www.instagram.com/weakutilityartist/",
        log=logs.append,
    )

    assert emails == []
    assert source_url == ""
    assert extract_method == "regex"
    assert onehop_target == ""
    assert fetch_calls == []
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_info url=https://developers.facebook.com/docs/instagram")
    _assert_log_contains(logs, "[IG OneHop] onehop_selected_target=https://developers.facebook.com/docs/instagram")
    _assert_log_contains(
        logs,
        "[IG OneHop] onehop_fetch_skipped reason=no_meaningful_target url=https://developers.facebook.com/docs/instagram",
    )
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=")


def test_instagram_onehop_emails_from_surface_still_fetches_meaningful_target(monkeypatch):
    logs = []
    fetch_calls = []
    target_url = "https://linktr.ee/meaningfulartist"

    def fake_fetch_website_html_bounded(session, url, **kwargs):  # noqa: ANN001
        fetch_calls.append(url)
        return cde.WebsiteFetchResult(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: meaningful@artist.com</body></html>",
            is_html=True,
        )

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch_website_html_bounded)

    emails, source_url, extract_method, onehop_target = cde._instagram_onehop_emails_from_surface(
        None,
        "<html><body>"
        "<a href='https://developers.facebook.com/docs/instagram'>Docs</a>"
        f"<a href='{target_url}'>Linktree</a>"
        "</body></html>",
        profile_url="https://www.instagram.com/meaningfulartist/",
        log=logs.append,
    )

    assert emails == ["meaningful@artist.com"]
    assert source_url == target_url
    assert extract_method == "regex"
    assert onehop_target == target_url
    assert fetch_calls == [target_url]
    _assert_log_contains(logs, f"[IG OneHop] ranked_target_selected tier=linkhub url={target_url}")
    _assert_log_contains(logs, f"[IG OneHop] onehop_selected_target={target_url}")
    _assert_log_contains(logs, f"[IG OneHop] onehop_fetch_attempted={target_url}")
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_skipped reason=no_meaningful_target")


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
    _assert_no_log_startswith(logs, "[IG Email] rejected_email_candidate")
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=non_empty count=1 sample=https://linktr.ee/onehopartist")
    _assert_log_contains(logs, "[IG OneHop] onehop_selected_target=https://linktr.ee/onehopartist")
    _assert_log_contains(logs, "[IG OneHop] onehop_fetch_attempted=https://linktr.ee/onehopartist")


def test_instagram_email_one_hop_rejects_asset_artifact_pseudo_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Artifact Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/artifactartist/",
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
            "<html><body><a href='https://distrokid.com/hyperfollow/artifactartist'>Bio</a></body></html>",
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
            html="<html><body>sweetalert2@8.min.js</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    _assert_log_contains(
        logs,
        "[IG Email] rejected_email_candidate reason=asset_artifact value=sweetalert2@8.min.js",
    )
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/artifactartist/",
        "[IG Email] no_email_visible",
    )


def test_instagram_email_one_hop_mixed_candidates_keep_real_email_and_reject_artifact(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Mixed Candidate Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/mixedcandidateartist/",
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
            "<html><body><a href='https://solo.to/mixedcandidateartist'>Bio</a></body></html>",
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
            html="<html><body>sweetalert2@8.min.js bookings@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "bookings@artist.com"
    assert seed_df.at[0, "Email_All"] == "bookings@artist.com"
    _assert_log_contains(
        logs,
        "[IG Email] rejected_email_candidate reason=asset_artifact value=sweetalert2@8.min.js",
    )
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/mixedcandidateartist/",
        "[IG Email] Found email: bookings@artist.com",
    )


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
    static_target = "https://beacons.ai/renderedlinkartist"
    live_target = "https://linktr.ee/renderedlinkartist"
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html=f"<html><body><a href='{static_target}'>Bio</a></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            f"<html><body><a href='{live_target}'>Bio</a><button>Email</button></body></html>",
            candidates=[{"text": "Email"}],
        ),
    )

    def fake_fetch_website_html_bounded(session, url, **kwargs):  # noqa: ANN001
        bio_fetch_calls.append(url)
        if url == static_target:
            return cde.WebsiteFetchResult(
                url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                html="<html><body>No email here</body></html>",
                is_html=True,
            )
        assert url == live_target
        return cde.WebsiteFetchResult(
            url=url,
            final_url=live_target,
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: rendered@artist.com</body></html>",
            is_html=True,
        )

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch_website_html_bounded)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert bio_fetch_calls == [static_target, live_target]
    assert seed_df.at[0, "Email"] == "rendered@artist.com"
    assert seed_df.at[0, "Email_All"] == "rendered@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == live_target
    assert "instagram_bio_link_one_hop" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_log_contains(logs, f"[IG OneHop] bio_link_urls state=non_empty count=1 sample={static_target}")
    _assert_log_contains(
        logs,
        f"[IG OneHop] live_surface_bio_link_urls state=non_empty count=1 sample={live_target}",
    )


def test_instagram_email_one_hop_live_surface_scopes_collection_away_from_document_chrome(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Scoped Live Surface Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/scopedlivesurfaceartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    bio_fetch_calls = []
    live_target = "https://linktr.ee/scopedlivesurfaceartist"
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Requests HTML without outbound bio link</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><head>"
            "<meta name='website' content='https://about.meta.com/'>"
            "</head><body>"
            "<nav><a href='https://help.instagram.com/12345'>Help</a></nav>"
            f"<main><a href='{live_target}'>Bio</a><button>Email</button></main>"
            "<footer><a href='https://www.threads.com/@scopedlivesurfaceartist'>Threads</a></footer>"
            "<script type='application/json'>"
            '{"profile":{"bio_links":[{"url":"https://beacons.ai/scopedlivesurfaceartist"}]}}'
            "</script>"
            "</body></html>"
        ),
    )

    def fake_fetch_website_html_bounded(session, url, **kwargs):  # noqa: ANN001
        bio_fetch_calls.append(url)
        assert url == live_target
        return cde.WebsiteFetchResult(
            url=url,
            final_url=live_target,
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: scoped-live@artist.com</body></html>",
            is_html=True,
        )

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch_website_html_bounded)

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert bio_fetch_calls == [live_target]
    assert seed_df.at[0, "Email"] == "scoped-live@artist.com"
    assert seed_df.at[0, "Email_All"] == "scoped-live@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == live_target
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(
        logs,
        f"[IG OneHop] live_surface_bio_link_urls state=non_empty count=1 sample={live_target}",
    )
    _assert_no_log_startswith(logs, "[IG OneHop] target_blocked reason=internal_meta")


def test_instagram_email_one_hop_live_surface_recovers_rendered_clickable_bio_link_control(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered Control Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/renderedcontrolartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    bio_fetch_calls = []
    target_url = "https://linktr.ee/renderedcontrolartist"
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Requests HTML without outbound bio link</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><header><button>Email</button></header></main></body></html>",
            live_bio_link_control_values=[
                "https://l.instagram.com/?u=https%3A%2F%2Flinktr.ee%2Frenderedcontrolartist&fbclid=abc123"
            ],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url=target_url,
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: rendered-control@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert bio_fetch_calls == [target_url]
    assert seed_df.at[0, "Email"] == "rendered-control@artist.com"
    assert seed_df.at[0, "Email_All"] == "rendered-control@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == target_url
    assert "instagram_bio_link_one_hop" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(logs, "[IG OneHop] live_clickable_candidates count=1")
    _assert_log_contains(
        logs,
        "[IG OneHop] live_surface_bio_link_urls state=non_empty count=1 sample=https://linktr.ee/renderedcontrolartist",
    )
    _assert_log_contains(logs, "[IG OneHop] onehop_selected_target=https://linktr.ee/renderedcontrolartist")


def test_instagram_email_one_hop_live_surface_runtime_adjoining_recovery_flows_through_existing_selection_and_fetch(
    monkeypatch,
):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Runtime Adjacent Control Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/runtimeadjacentcontrolartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    target_url = "https://linktr.ee/runtimeadjacentcontrolartist"
    bio_fetch_calls = []
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Requests HTML without outbound bio link</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><header><button>Email</button></header></main></body></html>",
            live_bio_link_control_values=[
                '{"runtime":{"target":"https://about.meta.com"}}',
                '{"runtime":{"target":"https://linktr.ee/runtimeadjacentcontrolartist"}}',
            ],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url=target_url,
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: runtime-adjacent@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert bio_fetch_calls == [target_url]
    assert seed_df.at[0, "Email"] == "runtime-adjacent@artist.com"
    assert seed_df.at[0, "Email_All"] == "runtime-adjacent@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == target_url
    _assert_log_contains(logs, "[IG OneHop] live_clickable_candidates count=2")
    _assert_log_contains(logs, "[IG OneHop] onehop_selected_target=https://linktr.ee/runtimeadjacentcontrolartist")
    _assert_log_contains(logs, "[IG OneHop] onehop_fetch_attempted=https://linktr.ee/runtimeadjacentcontrolartist")


def test_instagram_email_live_bridge_html_handoff_still_runs_one_hop_with_scoped_bio_link(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Handoff Bridge Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/handoffbridgeartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    target_url = "https://linktr.ee/handoffbridgeartist"
    static_html = (
        "<html><head><meta property='og:description' content='Official profile'></head>"
        "<body><div>Requests HTML without outbound bio link</div></body></html>"
    )
    live_html = (
        "<html><head><meta property='og:description' content='Official profile'></head>"
        "<body><main><header><h1>Handoff Bridge Artist</h1><button>Message</button></header>"
        f"<section><a href='{target_url}'>Bio</a></section></main></body></html>"
    )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", lambda session, url: (static_html, 200))
    _install_real_instagram_live_bridge(
        monkeypatch,
        landed_url="https://www.instagram.com/handoffbridgeartist/",
        html=live_html,
        landed_title="handoffbridgeartist (@handoffbridgeartist) • Instagram photos and videos",
        rendered_body_text="Handoff Bridge Artist Bio and booking details",
    )

    bio_fetch_calls = []
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url=target_url,
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: handoff-bridge@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert bio_fetch_calls == [target_url]
    assert seed_df.at[0, "Email"] == "handoff-bridge@artist.com"
    assert seed_df.at[0, "Email_All"] == "handoff-bridge@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == target_url
    assert "instagram_bio_link_one_hop" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(
        logs,
        f"[IG OneHop] live_surface_bio_link_urls state=non_empty count=1 sample={target_url}",
    )
    _assert_log_contains(logs, f"[IG OneHop] onehop_selected_target={target_url}")
    _assert_log_contains(logs, f"[IG OneHop] onehop_fetch_attempted={target_url}")


def test_instagram_email_live_bridge_html_handoff_preserves_direct_live_extraction_fallback(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Bridge Direct Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/bridgedirectartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    static_html = (
        "<html><head><meta property='og:description' content='Official profile'></head>"
        "<body><div>Requests HTML without contact details</div></body></html>"
    )
    live_html = (
        "<html><head>"
        "<meta property='og:description' content='Bookings: bridge-direct@artist.com'>"
        "</head><body><div>Logged-out profile payload</div></body></html>"
    )

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", lambda session, url: (static_html, 200))
    _install_real_instagram_live_bridge(
        monkeypatch,
        landed_url="https://www.instagram.com/bridgedirectartist/",
        html=live_html,
        landed_title="bridgedirectartist (@bridgedirectartist) • Instagram photos and videos",
        rendered_body_text="Logged-out profile payload",
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop fetch should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert seed_df.at[0, "Email"] == "bridge-direct@artist.com"
    assert seed_df.at[0, "Email_All"] == "bridge-direct@artist.com"
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_no_log_startswith(logs, "[IG OneHop] live_surface_bio_link_urls")
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=")


def test_instagram_email_static_one_hop_success_skips_live_retry(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Static Success Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/staticsuccessartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    static_target = "https://linktr.ee/staticsuccessartist"
    bio_fetch_calls = []
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html=f"<html><body><a href='{static_target}'>Bio</a></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><a href='https://linktr.ee/should-not-open'>Bio</a><button>Email</button></body></html>",
            candidates=[{"text": "Email"}],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url=static_target,
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: static-success@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert bio_fetch_calls == [static_target]
    assert live_pages == []
    assert seed_df.at[0, "Email"] == "static-success@artist.com"
    assert seed_df.at[0, "Email_All"] == "static-success@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == static_target
    _assert_log_contains(logs, f"[IG OneHop] bio_link_urls state=non_empty count=1 sample={static_target}")
    _assert_no_log_startswith(logs, "[IG OneHop] live_surface_bio_link_urls")


def test_instagram_email_one_hop_bio_link_recovers_direct_email_from_runtime_live_surface(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Runtime Link Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/runtimelinkartist/",
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
            "<html><body><button>Email</button></body></html>",
            runtime_window_payloads=[
                {
                    "web_profile_info": {
                        "bio_links": [{"url": "https://linktr.ee/runtimelinkartist"}]
                    }
                }
            ],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url="https://linktr.ee/runtimelinkartist",
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: runtime@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert bio_fetch_calls == ["https://linktr.ee/runtimelinkartist"]
    assert seed_df.at[0, "Email"] == "runtime@artist.com"
    assert seed_df.at[0, "Email_All"] == "runtime@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://linktr.ee/runtimelinkartist"
    assert "instagram_bio_link_one_hop" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(
        logs,
        "[IG OneHop] live_surface_bio_link_urls state=non_empty count=1 sample=https://linktr.ee/runtimelinkartist",
    )


def test_instagram_email_one_hop_live_runtime_waits_for_hydrated_payload_before_fetch(monkeypatch):
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(cde.time, "monotonic", clock)
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Deferred Runtime Link Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/deferredruntimelinkartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    bio_fetch_calls = []
    target_url = "https://linktr.ee/deferredruntimelinkartist"

    def make_live_page():
        page = _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Thin shell</div></main></body></html>",
            runtime_window_payloads=[],
        )
        page._clock = clock
        original_wait_for_timeout = page.wait_for_timeout

        def _hydrate_after_wait(timeout_ms):  # noqa: ANN001
            original_wait_for_timeout(timeout_ms)
            clock.advance(timeout_ms / 1000.0)
            page._runtime_window_payloads = [
                {"web_profile_info": {"bio_links": [{"url": target_url}]}}
            ]
            page._html = (
                "<html><body><main><a href='https://linktr.ee/deferredruntimelinkartist'>Bio</a></main></body></html>"
            )

        page.wait_for_timeout = _hydrate_after_wait
        return page

    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Requests HTML without outbound bio link</div></body></html>",
        live_page_factory=make_live_page,
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url=target_url,
            status=200,
            content_type="text/html",
            html="<html><body>Bookings: deferred-runtime@artist.com</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].wait_calls == [100]
    assert bio_fetch_calls == [target_url]
    assert seed_df.at[0, "Email"] == "deferred-runtime@artist.com"
    assert seed_df.at[0, "Email_All"] == "deferred-runtime@artist.com"
    assert seed_df.at[0, "Email_Source_URL"] == target_url
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(
        logs,
        "[IG OneHop] live_surface_bio_link_urls state=non_empty count=1 sample=https://linktr.ee/deferredruntimelinkartist",
    )


def test_instagram_onehop_emails_from_surface_recovers_direct_email_from_runtime_structured_payloads(monkeypatch):
    fetch_calls = []

    def fake_fetch_website_html_bounded(session, url, **kwargs):  # noqa: ANN001
        fetch_calls.append(url)
        pytest.fail("structured payload email should bypass one-hop URL fetch")

    monkeypatch.setattr(cde, "_fetch_website_html_bounded", fake_fetch_website_html_bounded)

    emails, source_url, extract_method, onehop_target = cde._instagram_onehop_emails_from_surface(
        None,
        "<html><body><div>No visible email</div></body></html>",
        profile_url="https://www.instagram.com/runtimeemailartist/",
        runtime_structured_payloads=[
            {
                "web_profile_info": {
                    "website": "https://linktr.ee/runtimeemailartist",
                    "business_email": "Bookings@Artist.com",
                    "headline": "booking enquiries welcome",
                }
            }
        ],
    )

    assert emails == ["bookings@artist.com"]
    assert source_url == "https://www.instagram.com/runtimeemailartist/"
    assert extract_method == "regex"
    assert onehop_target == ""
    assert fetch_calls == []


def test_collect_instagram_bio_equivalent_structured_texts_limits_to_bio_equivalent_fields():
    payloads = [
        {
            "web_profile_info": {
                "biography": "Bookings: bioonly@artist.com",
                "headline": "headline@artist.com",
                "bio_links": [{"url": "https://linktr.ee/bioonlyartist"}],
                "biography_with_entities": {
                    "raw_text": "Press: press@artist.com",
                    "entities": [{"url": "https://artist.com/contact"}],
                },
            }
        }
    ]

    texts = cde._collect_instagram_bio_equivalent_structured_texts(payloads)

    assert texts == [
        "Bookings: bioonly@artist.com",
        "Press: press@artist.com",
    ]


def test_instagram_email_live_direct_uses_runtime_structured_bio_text_when_rendered_surfaces_are_empty(
    monkeypatch,
):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Structured Bio Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/structuredbioartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text="",
            rendered_body_text_content="",
            rendered_main_text="",
            rendered_document_text_content="",
            rendered_aggregated_text="",
            runtime_structured_payloads=[
                {
                    "web_profile_info": {
                        "bio_links": [{"url": "https://linktr.ee/structuredbioartist"}],
                        "biography": "Bookings: structuredbio@artist.com",
                    }
                }
            ],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == "structuredbio@artist.com"
    assert seed_df.at[0, "Email_All"] == "structuredbio@artist.com"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    assert "instagram_bio_link_one_hop" not in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/structuredbioartist/",
        "[IG Email] Found email: structuredbio@artist.com",
    )


def test_instagram_email_live_direct_promotes_runtime_payload_direct_email_field(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Runtime Direct Email Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/runtimedirectemailartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text="",
            rendered_body_text_content="",
            rendered_main_text="",
            rendered_document_text_content="",
            rendered_aggregated_text="",
            runtime_structured_payloads=[
                {
                    "user": {
                        "public_email": "DirectRuntime@Artist.com",
                    }
                }
            ],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == "directruntime@artist.com"
    assert seed_df.at[0, "Email_All"] == "directruntime@artist.com"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    assert "instagram_bio_link_one_hop" not in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_log_contains(logs, "[IG Email] runtime_payload_surface state=non_empty count=1")
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/runtimedirectemailartist/",
        "[IG Email] Found email: directruntime@artist.com",
    )


def test_instagram_email_live_direct_promotes_runtime_payload_text_into_existing_parser(
    monkeypatch,
):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Runtime Text Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/runtimetextartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text="",
            rendered_body_text_content="",
            rendered_main_text="",
            rendered_document_text_content="",
            rendered_aggregated_text="",
            runtime_structured_payloads=[
                {
                    "user": {
                        "headline": "Bookings: runtimetext@artist.com",
                    }
                }
            ],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == "runtimetext@artist.com"
    assert seed_df.at[0, "Email_All"] == "runtimetext@artist.com"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    assert "instagram_bio_link_one_hop" not in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_log_contains(logs, "[IG Email] runtime_payload_surface state=non_empty count=1")
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/runtimetextartist/",
        "[IG Email] Found email: runtimetext@artist.com",
    )


def test_instagram_email_runtime_payload_direct_hit_skips_live_surface_onehop(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Runtime OneHop Short Circuit Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/runtimeonehopshortcircuit/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email or bio link</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text="",
            rendered_body_text_content="",
            rendered_main_text="",
            rendered_document_text_content="",
            rendered_aggregated_text="",
            runtime_structured_payloads=[
                {
                    "user": {
                        "public_email": "shortcircuit@artist.com",
                        "bio_links": [{"url": "https://linktr.ee/runtimeonehopshortcircuit"}],
                    }
                }
            ],
        ),
    )
    onehop_state_labels = []
    real_onehop = cde._instagram_onehop_emails_from_surface

    monkeypatch.setattr(
        cde,
        "_instagram_onehop_emails_from_surface",
        lambda *args, **kwargs: onehop_state_labels.append(
            kwargs.get("state_label", "bio_link_urls")
        ) or real_onehop(*args, **kwargs),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live one-hop fetch should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert onehop_state_labels == ["bio_link_urls"]
    assert seed_df.at[0, "Email"] == "shortcircuit@artist.com"
    assert seed_df.at[0, "Email_All"] == "shortcircuit@artist.com"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    assert "instagram_bio_link_one_hop" not in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_no_log_startswith(logs, "[IG OneHop] live_surface_bio_link_urls")
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=")
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/runtimeonehopshortcircuit/",
        "[IG Email] Found email: shortcircuit@artist.com",
    )


def test_instagram_email_live_direct_uses_shared_live_html_before_live_surface_onehop(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Shared Live Html Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/sharedlivehtmlartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email or outbound bio link</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Bookings: sharedlivehtml@artist.com</div></main></body></html>",
            runtime_structured_payloads=[
                {
                    "web_profile_info": {
                        "bio_links": [{"url": "https://linktr.ee/sharedlivehtmlartist"}],
                    }
                }
            ],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == "sharedlivehtml@artist.com"
    assert seed_df.at[0, "Email_All"] == "sharedlivehtml@artist.com"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    assert "instagram_bio_link_one_hop" not in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/sharedlivehtmlartist/",
        "[IG Email] Found email: sharedlivehtml@artist.com",
    )


def test_instagram_email_live_direct_uses_rendered_text_when_snapshot_html_misses_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered Text Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/renderedtextartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div><button>Email</button></main></body></html>",
            rendered_text="Bookings: renderedtext@artist.com",
            candidates=[{"text": "Email"}],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == "renderedtext@artist.com"
    assert seed_df.at[0, "Email_All"] == "renderedtext@artist.com"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/renderedtextartist/",
        "[IG Email] Found email: renderedtext@artist.com",
    )


def test_instagram_email_rendered_text_fallback_still_beats_runtime_structured_bio_text(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered Beats Structured Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/renderedbeatsstructured/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text="Bookings: renderedwins@artist.com",
            rendered_body_text_content="",
            rendered_main_text="",
            rendered_document_text_content="",
            rendered_aggregated_text="",
            runtime_structured_payloads=[
                {
                    "web_profile_info": {
                        "biography": "Bookings: structuredbutloses@artist.com",
                    }
                }
            ],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == "renderedwins@artist.com"
    assert seed_df.at[0, "Email_All"] == "renderedwins@artist.com"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]


def test_instagram_email_rendered_text_fallback_uses_best_non_empty_runtime_surface(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered Surface Priority Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/renderedsurfacepriority/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text="",
            rendered_body_text_content="Bookings: bodytext@artist.com",
            rendered_main_text="Bookings: mainsurface@artist.com",
            rendered_document_text_content="Bookings: document@artist.com",
            rendered_aggregated_text="Bookings: aggregated@artist.com",
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == "mainsurface@artist.com"
    assert seed_df.at[0, "Email_All"] == "mainsurface@artist.com"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]


def test_instagram_email_runtime_structured_bio_text_empty_still_falls_through_to_no_email_visible(
    monkeypatch,
):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Structured Empty Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/structuredemptyartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text="",
            rendered_body_text_content="",
            rendered_main_text="",
            rendered_document_text_content="",
            rendered_aggregated_text="",
            runtime_structured_payloads=[
                {
                    "web_profile_info": {
                        "bio_links": [{"url": "https://about.meta.com/"}],
                    }
                }
            ],
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("internal meta target should not fetch")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/structuredemptyartist/",
        "[IG Email] no_email_visible",
    )


def test_instagram_email_rendered_text_fallback_preserves_body_priority(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered Body Winner Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/renderedbodywinner/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text="Bookings: bodywinner@artist.com",
            rendered_body_text_content="Bookings: bodytext@artist.com",
            rendered_main_text="Bookings: mainsurface@artist.com",
            rendered_document_text_content="Bookings: document@artist.com",
            rendered_aggregated_text="Bookings: aggregated@artist.com",
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == "bodywinner@artist.com"
    assert seed_df.at[0, "Email_All"] == "bodywinner@artist.com"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]


def test_instagram_email_rendered_text_fallback_normalizes_stylized_unicode_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    stylized_email = "𝙇𝙖𝙘𝙚𝙙𝙪𝙥𝙈𝙂𝙈𝙏@𝙜𝙢𝙖𝙞𝙡.𝙘𝙤𝙢"
    normalized_rendered_email = "LacedupMGMT@gmail.com"
    extracted_email = "lacedupmgmt@gmail.com"
    assert unicodedata.normalize("NFKC", stylized_email) == normalized_rendered_email
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered Stylized Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/renderedstylizedartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text=f"Bookings: {stylized_email}",
            rendered_body_text_content="",
            rendered_main_text="",
            rendered_document_text_content="",
            rendered_aggregated_text="",
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == extracted_email
    assert seed_df.at[0, "Email_All"] == extracted_email
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/renderedstylizedartist/",
        f"[IG Email] Found email: {extracted_email}",
    )


def test_instagram_email_rendered_text_fallback_skips_when_all_runtime_surfaces_are_empty(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Rendered Empty Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/renderedemptyartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html="<html><body><div>Static shell without email</div></body></html>",
        live_page_factory=lambda: _DummyInstagramHiddenContactPage(
            "<html><body><main><div>Rendered profile without visible email in snapshot</div></main></body></html>",
            rendered_body_inner_text="",
            rendered_body_text_content="",
            rendered_main_text="",
            rendered_document_text_content="",
            rendered_aggregated_text="",
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == []
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/renderedemptyartist/",
        "[IG Email] no_email_visible",
    )


def test_instagram_email_bridge_failure_direct_fallback_recovers_from_static_structured_bio_text(
    monkeypatch,
):
    logs = []
    worker = _make_worker(logs)
    stylized_email = "𝙇𝙖𝙘𝙚𝙙𝙪𝙥𝙈𝙂𝙈𝙏@𝙜𝙢𝙖𝙞𝙡.𝙘𝙤𝙢"
    seed_df = _seed_df(
        {
            "Artist Name": "Bridge Structured Text Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/bridgestructuredtextartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    static_html = (
        "<html><body><div>Logged-out SSR shell without clickable bio link</div>"
        "<script type='application/json'>"
        '{"web_profile_info":{"biography":"Bookings: '
        + stylized_email
        + '"}}'
        "</script></body></html>"
    )
    bridge_calls = []
    onehop_state_labels = []
    real_onehop = cde._instagram_onehop_emails_from_surface

    monkeypatch.setattr(cde, "_fetch_instagram_profile_html", lambda session, url: (static_html, 200))
    monkeypatch.setattr(
        cde,
        "_open_instagram_live_page_bridge",
        lambda *args, **kwargs: bridge_calls.append(args[0]) or None,
    )
    monkeypatch.setattr(
        cde,
        "_instagram_onehop_emails_from_surface",
        lambda *args, **kwargs: onehop_state_labels.append(
            kwargs.get("state_label", "bio_link_urls")
        ) or real_onehop(*args, **kwargs),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop fetch should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert bridge_calls == ["https://www.instagram.com/bridgestructuredtextartist/"]
    assert onehop_state_labels == ["bio_link_urls"]
    assert seed_df.at[0, "Email"] == "lacedupmgmt@gmail.com"
    assert seed_df.at[0, "Email_All"] == "lacedupmgmt@gmail.com"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.instagram.com/bridgestructuredtextartist/"
    assert seed_df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert seed_df.at[0, "Email_Type"] == "ig_enrich"
    assert "instagram_profile" in seed_df.at[0, EMAIL_PROVENANCE_JSON_COL]
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_no_log_startswith(logs, "[IG OneHop] live_surface_bio_link_urls")
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=")


def test_instagram_email_no_live_bridge_handoff_preserves_empty_surface_fallback(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Blocked Bridge Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/blockedbridgeartist/",
            "Email_Source_URL": "",
            "Email_Source_Type": "",
            "Email_Extract_Method": "",
            "Email_Type": "",
            EMAIL_PROVENANCE_JSON_COL: "",
        }
    )
    ctx = worker._build_row_context(seed_df, 0, 1, 1)
    profile_shell_html = (
        "<html><body><main><header><h1>Blocked Bridge Artist</h1><button>Email</button></header></main></body></html>"
    )
    bridge_calls = []
    onehop_state_labels = []
    real_onehop = cde._instagram_onehop_emails_from_surface

    monkeypatch.setattr(
        cde,
        "_fetch_instagram_profile_html",
        lambda session, url: (profile_shell_html, 200),
    )
    monkeypatch.setattr(
        cde,
        "_open_instagram_live_page_bridge",
        lambda *args, **kwargs: bridge_calls.append(args[0]) or None,
    )
    monkeypatch.setattr(
        cde,
        "_instagram_onehop_emails_from_surface",
        lambda *args, **kwargs: onehop_state_labels.append(
            kwargs.get("state_label", "bio_link_urls")
        ) or real_onehop(*args, **kwargs),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("one-hop should not run")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert bridge_calls == ["https://www.instagram.com/blockedbridgeartist/"]
    assert onehop_state_labels == ["bio_link_urls"]
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    _assert_ig_visit_and_outcome(
        logs,
        "https://www.instagram.com/blockedbridgeartist/",
        "[IG Email] no_email_visible",
    )
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")


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
    static_target = "https://beacons.ai/sharedsurfaceartist"
    bio_fetch_calls = []
    live_pages = _install_instagram_profile_fetch_scope(
        monkeypatch,
        static_html=f"<html><body><a href='{static_target}'>Bio</a></body></html>",
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
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda session, url, **kwargs: bio_fetch_calls.append(url) or cde.WebsiteFetchResult(
            url=url,
            final_url=static_target,
            status=200,
            content_type="text/html",
            html="<html><body>No email on static one-hop target</body></html>",
            is_html=True,
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert bio_fetch_calls == [static_target]
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-0"]']
    assert seed_df.at[0, "Email"] == "shared@artist.com"
    assert "instagram_hidden_contact_one_action" in seed_df.at[0, "Email_Provenance_JSON"]
    _assert_log_contains(logs, f"[IG OneHop] bio_link_urls state=non_empty count=1 sample={static_target}")
    _assert_log_contains(logs, "[IG OneHop] live_surface_bio_link_urls state=empty count=0 sample=-")


def test_instagram_hidden_contact_one_action_runs_after_live_runtime_onehop_falls_through(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Runtime Shared Surface Hidden Contact Artist",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/runtimesharedsurfaceartist/",
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
                    "<html><body><div role='dialog'>Bookings: runtime-shared@artist.com</div></body></html>"
                )
            },
            runtime_window_payloads=[
                {"web_profile_info": {"website": "https://about.meta.com/"}}
            ],
        ),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is True
    assert len(live_pages) == 1
    assert live_pages[0].click_calls == ['[data-ig-hidden-contact="ig-hidden-contact-0"]']
    assert seed_df.at[0, "Email"] == "runtime-shared@artist.com"
    assert "instagram_hidden_contact_one_action" in seed_df.at[0, "Email_Provenance_JSON"]
    _assert_log_contains(logs, "[IG OneHop] bio_link_urls state=empty count=0 sample=-")
    _assert_log_contains(
        logs,
        "[IG OneHop] live_surface_bio_link_urls state=non_empty count=1 sample=https://about.meta.com",
    )
    _assert_log_contains(logs, "[IG OneHop] target_blocked reason=internal_meta url=https://about.meta.com")
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=")


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


def test_instagram_email_one_hop_weak_utility_only_target_resolves_to_clean_no_email(monkeypatch):
    logs = []
    worker = _make_worker(logs)
    seed_df = _seed_df(
        {
            "Artist Name": "Weak Utility Only",
            "Email": "",
            "Email_All": "",
            "Instagram_URL": "https://instagram.com/weakutilityonly/",
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
            "<a href='https://developers.facebook.com/docs/instagram'>Docs</a>"
            "<a href='https://www.meta.ai/?utm_source=foa_web_footer'>Meta AI</a>"
            "<a href='https://www.threads.com'>Threads</a>"
            "</body></html>",
            200,
        ),
    )
    monkeypatch.setattr(
        cde,
        "_fetch_website_html_bounded",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("weak utility-only winner should not fetch")),
    )

    matched = worker._enrich_row_instagram_email(seed_df, 0, ctx)

    assert matched is False
    assert seed_df.at[0, "Email"] == ""
    assert seed_df.at[0, "Email_All"] == ""
    _assert_log_contains(logs, "[IG OneHop] ranked_target_selected tier=external_info url=https://developers.facebook.com/docs/instagram")
    _assert_log_contains(logs, "[IG OneHop] onehop_selected_target=https://developers.facebook.com/docs/instagram")
    _assert_log_contains(
        logs,
        "[IG OneHop] onehop_fetch_skipped reason=no_meaningful_target url=https://developers.facebook.com/docs/instagram",
    )
    _assert_no_log_startswith(logs, "[IG OneHop] onehop_fetch_attempted=")
    _assert_log_contains(logs, "[IG Email] no_email_visible")


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
