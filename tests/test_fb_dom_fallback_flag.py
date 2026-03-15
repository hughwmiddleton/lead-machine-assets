import os

import pytest

import facebook_enrich


HTML_FIXTURE = """
<div role="main">
  <div aria-label="Search results">
    <div class="card">No anchors here</div>
  </div>
</div>
<article role="article">
  <a href="https://www.facebook.com/testpage">Test Page</a>
</article>
"""


def test_dom_fallback_disabled_returns_empty(monkeypatch):
    monkeypatch.delenv("NIGHT_FB_DOM_FALLBACK", raising=False)
    candidates = facebook_enrich._fb_extract_candidates_from_search_dom(HTML_FIXTURE, search_name="Test Page")
    assert candidates == []


def test_dom_fallback_enabled_augments_candidates(monkeypatch):
    monkeypatch.setenv("NIGHT_FB_DOM_FALLBACK", "1")
    candidates = facebook_enrich._fb_extract_candidates_from_search_dom(HTML_FIXTURE, search_name="Test Page")
    urls = [c.url for c in candidates]
    assert "https://www.facebook.com/testpage" in urls


@pytest.mark.parametrize(
    "bad_href",
    [
        "https://www.facebook.com/photo.php?fbid=123",
        "https://www.facebook.com/story.php?story_fbid=123&id=456",
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456",
        "https://www.facebook.com/sharer.php?u=https%3A%2F%2Fexample.com",
    ],
)
def test_dom_fallback_enabled_still_rejects_non_page_urls(monkeypatch, bad_href):
    monkeypatch.setenv("NIGHT_FB_DOM_FALLBACK", "1")
    html = f"""
    <div role="main">
      <div aria-label="Search results">
        <div class="card">No anchors here</div>
      </div>
    </div>
    <article role="article">
      <a href="{bad_href}">Test Page</a>
    </article>
    """
    candidates = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Test Page")
    assert candidates == []
