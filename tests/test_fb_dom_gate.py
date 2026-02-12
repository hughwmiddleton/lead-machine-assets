import facebook_enrich


def test_dom_gate_fallback_outside_main_scope():
    html = """
    <div aria-label="Search results">
      <a href="https://www.facebook.com/testband">Test Band</a>
    </div>
    """
    cands = facebook_enrich._fb_extract_candidates_from_search_dom(html, search_name="Test Band")
    assert len(cands) == 1
    assert cands[0].url == "https://www.facebook.com/testband"
