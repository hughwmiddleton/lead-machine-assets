import pytest

import facebook_enrich
import night_mode_fb


class _ImmediateWait:
    def __init__(self, driver, *_args, **_kwargs):
        self.driver = driver

    def until(self, condition):
        return condition(self.driver)


class _FakeAnchor:
    def __init__(self, href: str):
        self._href = href
        self.id = f"anchor-{id(self)}"

    def get_attribute(self, name: str):
        if name == "href":
            return self._href
        return ""


class _FakeContainer:
    def __init__(self, *, anchor_hrefs=None, role_link_hrefs=None, html: str = ""):
        self._anchor_elements = [_FakeAnchor(href) for href in (anchor_hrefs or [])]
        self._role_link_elements = [_FakeAnchor(href) for href in (role_link_hrefs or [])]
        self._html = html

    def find_elements(self, _by, selector):
        if selector == "a[href]":
            return list(self._anchor_elements)
        if selector == 'a[role="link"][href]':
            return []
        if selector == '[role="link"]':
            return list(self._role_link_elements)
        return []

    def get_attribute(self, name: str):
        if name in ("innerHTML", "outerHTML"):
            return self._html
        return ""


class _FakeDriver:
    def __init__(self, *, page_source: str, selector_map):
        self.page_source = page_source
        self.current_url = "https://www.facebook.com/search/pages/?q=test"
        self._selector_map = dict(selector_map)

    def find_elements(self, _by, selector):
        return list(self._selector_map.get(selector, []))

    def execute_script(self, _script, *_args):
        return None


def test_dom_gate_fallback_outside_main_scope():
    html = """
    <div aria-label="Search results">
      <a href="https://www.facebook.com/testband">Test Band</a>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Test Band")
    assert len(cands) == 1
    assert cands[0].url == "https://www.facebook.com/testband"


def test_dom_gate_v2_fallbacks_to_data_pagelet():
    html = """
    <div role="main">
      <div aria-label="Search results">
        <div>No anchors here</div>
      </div>
      <div data-pagelet="SearchResults">
        <a href="https://www.facebook.com/datapageletband">Data Pagelet Band</a>
      </div>
    </div>
    """
    selector, anchors, pre_gate, hrefs = night_mode_fb._collect_search_candidates_from_html_v2(html)
    assert selector == 'div[role="main"] [data-pagelet^="SearchResults"]'
    assert anchors >= 1
    assert pre_gate == 1
    assert "https://www.facebook.com/datapageletband" in hrefs


def test_dom_gate_v2_fallbacks_to_article_in_main():
    html = """
    <div role="main">
      <div role="article">
        <a href="https://www.facebook.com/articleband">Article Band</a>
      </div>
    </div>
    """
    selector, anchors, pre_gate, hrefs = night_mode_fb._collect_search_candidates_from_html_v2(html)
    assert selector == 'div[role="main"] div[role="article"]'
    assert anchors >= 1
    assert pre_gate == 1
    assert "https://www.facebook.com/articleband" in hrefs


def test_dom_gate_v2_zero_anchors_stays_zero():
    html = """
    <div role="main">
      <div aria-label="Search results">
        <div class="card">No anchors</div>
      </div>
    </div>
    """
    selector, anchors, pre_gate, hrefs = night_mode_fb._collect_search_candidates_from_html_v2(html)
    assert anchors == 0
    assert pre_gate == 0
    assert hrefs == []


def test_dom_gate_allows_page_urls_with_tracking_query_wrappers():
    html = """
    <div aria-label="Search results">
      <a href="https://www.facebook.com/testband?__tn__=%3C">Test Band</a>
      <a href="https://www.facebook.com/profile.php?id=123456789&ref=share">Test Band Profile</a>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Test Band")
    urls = [cand.url for cand in cands]
    assert "https://www.facebook.com/testband?__tn__=%3C" in urls
    assert "https://www.facebook.com/profile.php?id=123456789&ref=share" in urls


@pytest.mark.parametrize(
    "bad_href",
    [
        "https://business.facebook.com/testband",
        "https://www.facebook.com/photo.php?fbid=123",
        "https://www.facebook.com/story.php?story_fbid=123&id=456",
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456",
        "https://www.facebook.com/sharer.php?u=https%3A%2F%2Fexample.com",
    ],
)
def test_dom_gate_rejects_non_page_search_result_urls(bad_href):
    html = f"""
    <div aria-label="Search results">
      <a href="{bad_href}">Test Band</a>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Test Band")
    assert cands == []


@pytest.mark.parametrize(
    "bad_href",
    [
        "https://business.facebook.com/latest/composer",
        "https://www.facebook.com/story.php?story_fbid=123&id=456",
        "https://www.facebook.com/photo.php?fbid=123",
    ],
)
def test_dom_gate_rejects_role_link_junk_urls(bad_href):
    html = f"""
    <div role="main">
      <div aria-label="Search results">
        <div role="link" data-href="{bad_href}">
          <a aria-label="Test Band">Test Band</a>
          <div class="subtitle">Musician/band</div>
        </div>
      </div>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Test Band")
    assert cands == []


def test_dom_gate_v2_zero_result_uses_shared_fallback_for_standalone_article(monkeypatch):
    monkeypatch.setenv("NIGHT_FB_DOM_FALLBACK", "1")
    monkeypatch.setattr(night_mode_fb, "WebDriverWait", _ImmediateWait)
    monkeypatch.setattr(night_mode_fb.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(night_mode_fb.random, "uniform", lambda a, _b: a)

    page_html = """
    <div role="main">
      <div aria-label="Search results">
        <div class="card">No anchors here</div>
      </div>
    </div>
    <article role="article">
      <a href="https://www.facebook.com/testpage">Test Page</a>
    </article>
    """
    main_container = _FakeContainer(html="<div aria-label='Search results'><div class='card'>No anchors here</div></div>")
    driver = _FakeDriver(
        page_source=page_html,
        selector_map={
            'div[role="main"]': [main_container],
        },
    )

    candidates = night_mode_fb._harvest_search_candidates_v2(driver, search_name="Test Page")

    assert [cand.url for cand in candidates] == ["https://www.facebook.com/testpage"]


def test_dom_gate_v2_zero_result_shared_fallback_still_rejects_junk(monkeypatch):
    monkeypatch.setenv("NIGHT_FB_DOM_FALLBACK", "1")
    monkeypatch.setattr(night_mode_fb, "WebDriverWait", _ImmediateWait)
    monkeypatch.setattr(night_mode_fb.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(night_mode_fb.random, "uniform", lambda a, _b: a)

    page_html = """
    <div role="main">
      <div aria-label="Search results">
        <div class="card">No anchors here</div>
      </div>
    </div>
    <article role="article">
      <a href="https://www.facebook.com/photo.php?fbid=123">Photo</a>
    </article>
    """
    main_container = _FakeContainer(html="<div aria-label='Search results'><div class='card'>No anchors here</div></div>")
    driver = _FakeDriver(
        page_source=page_html,
        selector_map={
            'div[role="main"]': [main_container],
        },
    )

    candidates = night_mode_fb._harvest_search_candidates_v2(driver, search_name="Test Page")

    assert candidates == []


def test_dom_gate_v2_primary_candidates_unchanged_when_already_present(monkeypatch):
    monkeypatch.setattr(night_mode_fb, "WebDriverWait", _ImmediateWait)
    monkeypatch.setattr(night_mode_fb.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(night_mode_fb.random, "uniform", lambda a, _b: a)

    page_html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://www.facebook.com/testartist">Test Artist</a>
      </div>
    </div>
    """
    search_container = _FakeContainer(
        anchor_hrefs=["https://www.facebook.com/testartist"],
        html="<a href='https://www.facebook.com/testartist'>Test Artist</a>",
    )
    driver = _FakeDriver(
        page_source=page_html,
        selector_map={
            'div[aria-label="Search results"]': [search_container],
        },
    )

    candidates = night_mode_fb._harvest_search_candidates_v2(driver, search_name="Test Artist")

    assert [cand.url for cand in candidates] == ["https://www.facebook.com/testartist"]
