import os

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
