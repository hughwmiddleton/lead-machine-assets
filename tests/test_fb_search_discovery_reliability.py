"""
Regression tests for Ticket 3: Facebook search-result discovery reliability.

Focus: layered extractor, selector broadening, href normalization,
rejection accounting, and slug fallback preservation.
"""

from __future__ import annotations

import pytest

import facebook_enrich


# ---------------------------------------------------------------------------
# 1. Search surface extraction
# ---------------------------------------------------------------------------

def test_layered_extractor_survives_mixed_search_surface():
    """
    Fixture contains:
    - artist public page link
    - navigation/internal link
    - duplicate artist link
    - relative artist link

    Only valid artist-page candidates survive; duplicates collapse.
    """
    html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://www.facebook.com/testartist">Test Artist</a>
        <a href="https://www.facebook.com/search/top/?q=test">More results</a>
        <a href="https://www.facebook.com/testartist">Test Artist</a>
        <a href="/testartist2">Test Artist Two</a>
      </div>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Test Artist")
    urls = [c.url for c in cands]
    assert "https://www.facebook.com/testartist" in urls
    assert "https://www.facebook.com/testartist2" in urls
    # Navigation/search links rejected
    assert "https://www.facebook.com/search/top/?q=test" not in urls
    # Duplicate collapsed
    assert urls.count("https://www.facebook.com/testartist") == 1


# ---------------------------------------------------------------------------
# 2. Narrow scope fallback
# ---------------------------------------------------------------------------

def test_narrow_scope_empty_falls_back_to_broader_search_scope():
    """
    Primary narrow scope (div[aria-label='Search results']) exists but has
    zero usable hrefs. Broader bounded scope (role=article) contains a valid
    artist link. Layered extractor must return the valid candidate instead of
    logging zero_usable_hrefs_in_scope immediately.
    """
    html = """
    <div role="main">
      <div aria-label="Search results">
        <div class="card">No anchors here</div>
      </div>
      <div role="article">
        <a href="https://www.facebook.com/articleartist">Article Artist</a>
      </div>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Article Artist")
    urls = [c.url for c in cands]
    assert "https://www.facebook.com/articleartist" in urls


# ---------------------------------------------------------------------------
# 3. Relative URL normalization
# ---------------------------------------------------------------------------

def test_relative_href_normalized_to_canonical():
    """
    A legitimate relative Facebook public-page href must become the expected
    canonical candidate URL.
    """
    html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="/relativeband">Relative Band</a>
      </div>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Relative Band")
    urls = [c.url for c in cands]
    assert "https://www.facebook.com/relativeband" in urls


# ---------------------------------------------------------------------------
# 4. Rejection accounting
# ---------------------------------------------------------------------------

def test_diagnostics_distinguish_raw_hrefs_from_zero_usable(monkeypatch):
    """
    When raw links exist but all are rejected by the URL gate, logs must show
    candidates_pre_url_gate > 0 and candidates_post_url_gate == 0, not
    candidates_pre_url_gate == 0.
    """
    html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://www.facebook.com/photo.php?fbid=123">Photo</a>
        <a href="https://www.facebook.com/watch?v=456">Watch</a>
      </div>
    </div>
    """
    logs: list[str] = []
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(
        html, logger=logs.append, search_name="Reject All"
    )
    assert cands == []
    dom_gate_log = ""
    for msg in logs:
        if "[FB Shared][DOM Gate]" in msg and "reason=" in msg:
            dom_gate_log = msg
            break
    assert "candidates_pre_url_gate=2" in dom_gate_log, f"Expected pre_gate > 0 in log: {dom_gate_log}"
    assert "candidates_post_url_gate=0" in dom_gate_log, f"Expected post_gate == 0 in log: {dom_gate_log}"
    assert "url_gate_rejected=2" in dom_gate_log, f"Expected gate reject count in log: {dom_gate_log}"


# ---------------------------------------------------------------------------
# 5. Async / readiness behaviour (architecture permits via driver param)
# ---------------------------------------------------------------------------

class _FakeDriverWithLateAnchors:
    """Driver that populates anchors after the first page_source read."""

    def __init__(self, late_html: str):
        self._initial = True
        self._early_html = """
        <div role="main">
          <div aria-label="Search results">
            <div class="card">Loading...</div>
          </div>
        </div>
        """
        self._late_html = late_html

    @property
    def page_source(self):
        if self._initial:
            self._initial = False
            return self._early_html
        return self._late_html


def test_bounded_wait_recovers_async_anchors():
    """
    First page_source has the result surface but no links.
    After a bounded wait, page_source contains a valid result link.
    The extractor should return the candidate without invoking slug fallback.
    """
    late_html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://www.facebook.com/asyncartist">Async Artist</a>
      </div>
    </div>
    """
    driver = _FakeDriverWithLateAnchors(late_html)
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(
        driver, search_name="Async Artist"
    )
    urls = [c.url for c in cands]
    assert "https://www.facebook.com/asyncartist" in urls


# ---------------------------------------------------------------------------
# 6. Slug fallback preservation
# ---------------------------------------------------------------------------

def test_slug_fallback_runs_when_search_discovery_yields_nothing(monkeypatch):
    """
    If the extractor truly returns no usable candidates, the existing slug
    fallback in FacebookSearchClient.find_best_page_url must still run.
    """
    pytest.importorskip("PyQt5")
    import cross_directory_enricher as cde

    class _FakeDriver:
        def __init__(self):
            self.current_url = "https://www.facebook.com/search/pages/?q=test"
            self.page_source = ""
            self.visited_urls: list[str] = []

        def get(self, url):
            self.visited_urls.append(url)
            self.current_url = url

        def find_element(self, by=None, value=None):
            if value == "body":
                return object()
            raise Exception("unexpected")

    driver = _FakeDriver()
    client = cde.FacebookSearchClient(driver=driver, logger=None)
    monkeypatch.setattr(client, "ensure_facebook_logged_in", lambda: True)

    def _fake_fetch(query, *, search_method):
        return (
            "<div role='main'><div aria-label='Search results'><div class='card'>No anchors</div></div></div>",
            "https://www.facebook.com/search/pages/?q=test",
            False,
        )

    monkeypatch.setattr(client, "_fetch_search_surface", _fake_fetch)

    result = client.find_best_page_url("SlugFallbackBand", require_strong_candidate=False)

    # Slug fallback produces lower-case slug from normalized name.
    assert result == "https://www.facebook.com/slugfallbackband"


# ---------------------------------------------------------------------------
# 7. Existing qualification gates remain intact
# ---------------------------------------------------------------------------

def test_layered_extractor_does_not_bypass_url_safety_gates():
    """
    Even with broader scopes, extraction improvements must not admit URLs that
    fail the existing public-route safety gates.
    """
    html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://www.facebook.com/groups/badgroup">Bad Group</a>
        <a href="https://www.facebook.com/events/123/bad">Bad Event</a>
        <a href="https://www.facebook.com/watch?v=456">Bad Watch</a>
      </div>
      <div role="article">
        <a href="https://www.facebook.com/photo.php?fbid=789">Bad Photo</a>
      </div>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Bad")
    assert cands == []


def test_layered_extractor_preserves_profile_php_id_form():
    """
    Supported profile.php?id=... forms must continue to survive the gate.
    """
    html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://www.facebook.com/profile.php?id=123456789">Profile Artist</a>
      </div>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Profile Artist")
    urls = [c.url for c in cands]
    assert "https://www.facebook.com/profile.php?id=123456789" in urls


def test_redirect_wrapper_resolved_safely():
    """
    Facebook redirect wrappers (l.facebook.com) must be resolved to their
    destination and then validated through the same safety gates.
    """
    html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.facebook.com%2Fwrappedband">Wrapped</a>
      </div>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Wrapped")
    urls = [c.url for c in cands]
    assert "https://www.facebook.com/wrappedband" in urls


def test_redirect_wrapper_to_junk_still_rejected():
    """
    A redirect wrapper pointing to a junk surface must still be rejected.
    """
    html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.facebook.com%2Fgroups%2Fbad">Wrapped Bad</a>
      </div>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Wrapped Bad")
    assert cands == []
