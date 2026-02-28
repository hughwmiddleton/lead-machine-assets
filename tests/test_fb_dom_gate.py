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
