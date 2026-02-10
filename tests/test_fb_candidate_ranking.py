from types import SimpleNamespace

import night_mode_fb
import importlib.util
from pathlib import Path


def _cand(name: str, url: str, category: str = "", aria: str = "", secondary: str = ""):
    obj = SimpleNamespace(name=name, url=url, category=category, aria_label=aria, secondary_text=secondary)
    return obj


def test_ranking_prefers_music_page_over_profile():
    artist = "Zipporah"
    c1 = _cand("Zipporah.co.ph", "https://www.facebook.com/zipporahcoph", "Local service")
    c2 = _cand("Zipporah (Official)", "https://www.facebook.com/zipporahmusic", "Musician/Band")
    c3 = _cand("Zipporah Hanz", "https://www.facebook.com/people/zipporah/123", "Musician/Band")

    ranked = night_mode_fb._rank_candidates_for_preview(artist, [c1, c2, c3])
    assert ranked[0]["candidate"] is c2  # music page wins
    assert ranked[1]["candidate"] is c3  # profile loses to page


def test_ranking_handles_name_match_and_non_music_category():
    artist = "Lunar Echoes"
    c1 = _cand("Lunar Echoes", "https://www.facebook.com/lunarechoes", "Coffee shop")  # non-music category
    c2 = _cand("Lunar Echoes Band", "https://www.facebook.com/lunarechoesband", "Musician/Band")
    ranked = night_mode_fb._rank_candidates_for_preview(artist, [c1, c2])
    top = ranked[0]
    assert top["candidate"] is c2
    # Ensure scoring penalty applied to non-music service category.
    score1, _, _ = night_mode_fb._score_fb_candidate_night(artist, c1)
    score2, _, _ = night_mode_fb._score_fb_candidate_night(artist, c2)
    assert score2 > score1


def test_page_style_url_variants_score_like_pages():
    artist = "Echo Star"
    page_slug = _cand("Echo Star", "https://www.facebook.com/echostar", "Musician/Band")
    page_pages = _cand("Echo Star", "https://www.facebook.com/pages/Echo-Star/123456", "Musician/Band")
    page_p = _cand("Echo Star", "https://www.facebook.com/p/echostar/123", "Musician/Band")
    profile_people = _cand("Echo Star", "https://www.facebook.com/people/Echo-Star/123456", "Musician/Band")

    assert night_mode_fb._is_page_style_url(page_slug.url)
    assert night_mode_fb._is_page_style_url(page_pages.url)
    assert night_mode_fb._is_page_style_url(page_p.url)
    assert not night_mode_fb._is_page_style_url(profile_people.url)

    score_page, _, _ = night_mode_fb._score_fb_candidate_night(artist, page_pages)
    score_profile, _, _ = night_mode_fb._score_fb_candidate_night(artist, profile_people)
    assert score_page > score_profile


def test_rank_preview_does_not_mutate_candidate_choice():
    artist = "Glow"
    cands = [_cand("Glow", "https://www.facebook.com/glowmusic", "Musician/Band"), _cand("Glow Music", "https://www.facebook.com/glowmusic2", "Artist")]
    chosen = cands[0]
    original_order = [c.url for c in cands]
    night_mode_fb._maybe_log_rank_preview(artist, cands, chosen, logger=lambda _: None)
    assert [c.url for c in cands] == original_order
    assert chosen is cands[0]


def test_order_candidates_unique_and_stable():
    a = _cand("A", "https://www.facebook.com/a", "Artist")
    b = _cand("B", "https://www.facebook.com/b", "Artist")
    c = _cand("C", "https://www.facebook.com/c", "Artist")
    ranked = [b, a]
    ordered_flag_off = night_mode_fb._order_candidates_for_selection(a, [a, b, c], [], False)
    assert ordered_flag_off == [a, b, c]
    assert len({id(x) for x in ordered_flag_off}) == len(ordered_flag_off)

    ordered_flag_on = night_mode_fb._order_candidates_for_selection(a, [a, b, c], ranked, True)
    assert ordered_flag_on[:2] == ranked  # ranked order preserved
    assert len({id(x) for x in ordered_flag_on}) == len(ordered_flag_on)


def test_sanitize_drops_reminder_prefix():
    assert night_mode_fb._sanitize_fb_category_text("Reminder: You have an event coming up soon") is None


def test_sanitize_drops_very_long_category():
    long_value = "x" * 81
    assert night_mode_fb._sanitize_fb_category_text(long_value) is None


def test_sanitize_keeps_valid_categories():
    assert night_mode_fb._sanitize_fb_category_text("Musician/band") == "Musician/band"
    assert night_mode_fb._sanitize_fb_category_text("Artist") == "Artist"


def test_category_equal_name_is_dropped_in_parse():
    html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://www.facebook.com/sofialy" aria-label="Profile">Sofia Ly</a>
        <div class="subtitle">Sofia Lý</div>
      </div>
    </div>
    """
    cands = night_mode_fb._parse_search_candidates(html, logger=None, search_name="Sofia Ly")
    assert cands
    assert getattr(cands[0], "category", "") == ""


def test_music_hint_from_aria_label_when_category_missing():
    html = """
    <div role="main">
      <div aria-label="Search results">
        <a href="https://www.facebook.com/djmoon" aria-label="Musician/band">DJ Moon</a>
      </div>
    </div>
    """
    cands = night_mode_fb._parse_search_candidates(html, logger=None, search_name="DJ Moon")
    assert cands
    assert getattr(cands[0], "music_hint", False) is True


def test_profile_music_hint_softens_penalty():
    artist = "Sofia Ly"
    profile = _cand("Sofia Ly", "https://www.facebook.com/profile.php?id=12345", "", aria="Musician/band")
    page = _cand("Sofia Ly Fan Club", "https://www.facebook.com/sofialyfanclub", "Community")
    ranked = night_mode_fb._rank_candidates_for_preview(artist, [page, profile])
    assert ranked[0]["candidate"] is profile


def test_parse_search_candidates_uses_card_category_and_music_hint():
    html = """
    <div role="main">
      <div aria-label="Search results">
        <div class="card">
          <a href="https://www.facebook.com/someband" aria-label="">Some Band</a>
          <div class="subtitle">Musician/band</div>
        </div>
      </div>
    </div>
    """
    cands = night_mode_fb._parse_search_candidates(html, logger=None, search_name="Some Band")
    assert len(cands) == 1
    cand = cands[0]
    assert getattr(cand, "category", "") == "Musician/band"
    assert getattr(cand, "music_hint", False) is True


def test_parse_search_candidates_does_not_duplicate_anchors():
    html = """
    <div role="main">
      <div aria-label="Search results">
        <div class="card">
          <a href="https://www.facebook.com/page.one">Page One</a>
          <div class="subtitle">Musician/band</div>
        </div>
        <div class="card">
          <a href="https://www.facebook.com/page.two">Page Two</a>
          <div class="subtitle">Artist</div>
        </div>
      </div>
    </div>
    """
    cands = night_mode_fb._parse_search_candidates(html, logger=None, search_name="Page")
    assert len(cands) == 2


def test_is_profile_url_handles_queries_and_people_paths():
    assert night_mode_fb._is_profile_url("https://www.facebook.com/profile.php?id=123&__tn__=%3C")
    assert night_mode_fb._is_profile_url("https://www.facebook.com/people/some-name/1234567890")
    assert night_mode_fb._is_profile_url("https://www.facebook.com/people/some-name/1234567890?sk=about")


def _load_legacy_module():
    path = Path(__file__).resolve().parents[1] / "Lead Machine (Final Update 5).py"
    spec = importlib.util.spec_from_file_location("lead_machine_legacy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_legacy_sanitize_drops_reminder():
    lm = _load_legacy_module()
    assert lm._legacy_sanitize_fb_category_text("Reminder: You have an event coming up soon") is None


def test_legacy_resolve_prefers_candidate_when_scraped_is_reminder():
    lm = _load_legacy_module()
    sanitized, final_cat = lm._resolve_fb_page_category(
        "Reminder: You have an event coming up soon",
        "Musician/band",
    )
    assert sanitized is None
    assert final_cat == "Musician/band"


def test_legacy_resolve_prefers_scraped_when_valid():
    lm = _load_legacy_module()
    sanitized, final_cat = lm._resolve_fb_page_category("Artist", "Musician/band")
    assert sanitized == "Artist"
    assert final_cat == "Artist"
