import pytest

import facebook_enrich
import night_mode_fb


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
