from types import SimpleNamespace

import night_mode_fb
import importlib.util
from pathlib import Path


def _cand(name: str, url: str, category: str = "", aria: str = "", secondary: str = ""):
    obj = SimpleNamespace(name=name, url=url, category=category, aria_label=aria, secondary_text=secondary)
    return obj


def _load_fixture(name: str) -> str:
    fixtures = Path(__file__).resolve().parent / "fixtures"
    return (fixtures / name).read_text(encoding="utf-8")


def _dummy_driver():
    class DummyDriver:
        page_source = "<html></html>"
        current_url = "https://www.facebook.com"

        def get(self, url):  # pragma: no cover - trivial stub
            self.current_url = url
            self.page_source = "<html></html>"

    return DummyDriver()


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


def test_service_only_loses_to_music_even_with_exact_name():
    artist = "Shelailai"
    service = _cand("Shelailai", "https://www.facebook.com/shelailai", "Product/service")
    music = _cand("Shelailai Music", "https://www.facebook.com/shelailaimusic", "Musician/Band")
    ranked = night_mode_fb._rank_candidates_for_preview(artist, [service, music])
    assert ranked[0]["candidate"] is music


def test_mixed_service_and_music_beats_pure_service():
    artist = "Nova"
    service = _cand("Nova", "https://www.facebook.com/nova", "Product/service")
    mixed = _cand("Nova Band", "https://www.facebook.com/novaband", "Product/service", secondary="Musician/Band")
    ranked = night_mode_fb._rank_candidates_for_preview(artist, [service, mixed])
    assert ranked[0]["candidate"] is mixed
    score_service, _, _ = night_mode_fb._score_fb_candidate_night(artist, service)
    score_mixed, _, _ = night_mode_fb._score_fb_candidate_night(artist, mixed)
    assert score_mixed > score_service


def test_page_beats_profile_when_signals_equal():
    artist = "Echo Star"
    page = _cand("Echo Star", "https://www.facebook.com/echostar", "Musician/Band")
    profile = _cand("Echo Star", "https://www.facebook.com/profile.php?id=123456", "Musician/Band")
    ranked = night_mode_fb._rank_candidates_for_preview(artist, [profile, page])
    assert ranked[0]["candidate"] is page


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
    assert score_page >= score_profile


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


def test_sofia_ly_prefers_exact_profile_over_page_mismatch():
    artist = "Sofia Ly"
    profile = _cand("Sofia Lý", "https://www.facebook.com/profile.php?id=987654321", "Musician/band")
    page = _cand("LY SOFIA", "https://www.facebook.com/lysofiagroup", "Musician/band")
    corporate = _cand("Perfumes Originales Sofia Ly", "https://www.facebook.com/perfumesoriginales", "Health/beauty")

    ranked = night_mode_fb._rank_candidates_for_preview(artist, [profile, page, corporate])
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


def test_tracked_slug_candidate_survives_to_ranking_and_normalizes_on_selection():
    html = """
    <div role="main">
      <div aria-label="Search results">
        <div class="card">
          <a href="https://www.facebook.com/nightlightmusic?__tn__=%3C">Nightlight Music</a>
          <div class="subtitle">Musician/band</div>
        </div>
        <div class="card">
          <a href="https://business.facebook.com/nightlightmusic">Unread composer notice</a>
          <div class="subtitle">5h</div>
        </div>
        <div class="card">
          <a href="https://www.facebook.com/story.php?story_fbid=123&id=456">Story result</a>
        </div>
      </div>
    </div>
    """
    candidates = night_mode_fb._parse_search_candidates(html, logger=None, search_name="Nightlight")
    assert len(candidates) == 1
    assert candidates[0].url == "https://www.facebook.com/nightlightmusic?__tn__=%3C"

    ranked = night_mode_fb._rank_candidates_for_preview("Nightlight", candidates)
    assert ranked[0]["candidate"].url == "https://www.facebook.com/nightlightmusic?__tn__=%3C"

    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    selected = enricher._select_candidate_url(
        "Nightlight",
        ranked[0]["candidate"],
        candidates,
        [item["candidate"] for item in ranked],
        ranked,
        "ranked_sort",
        night_mode_fb._min_fb_accept_score(),
    )
    assert selected == "https://www.facebook.com/nightlightmusic"


def test_is_profile_url_handles_queries_and_people_paths():
    assert night_mode_fb._is_profile_url("https://www.facebook.com/profile.php?id=123&__tn__=%3C")
    assert night_mode_fb._is_profile_url("https://www.facebook.com/people/some-name/1234567890")
    assert night_mode_fb._is_profile_url("https://www.facebook.com/people/some-name/1234567890?sk=about")


def test_unsafe_candidate_filtered_before_navigation(monkeypatch):
    artist = "Aurora Beam"
    unsafe = _cand("American Dental Institute", "https://www.facebook.com/americandentalinstituteorlando", "Dental clinic")
    safe = _cand("Aurora Beam", "https://www.facebook.com/aurorabeammusic", "Musician/Band")
    candidates = [unsafe, safe]

    ranked_for_preview = [
        {
            "candidate": unsafe,
            "score": 70,
            "breakdown": [],
            "features": {
                "category": "Dental clinic",
                "descriptor": "",
                "aria_label": "",
                "secondary_text": "",
                "category_tokens": ["Dental", "Clinic"],
                "match_level": "near",
                "music_any": False,
                "service_only": True,
            },
        },
        {
            "candidate": safe,
            "score": 60,
            "breakdown": [],
            "features": {
                "category": "Musician/Band",
                "descriptor": "",
                "aria_label": "",
                "secondary_text": "",
                "category_tokens": ["Musician", "Band"],
                "match_level": "near",
                "music_any": True,
                "service_only": False,
            },
        },
    ]

    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: candidates)
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: ranked_for_preview)
    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_get_anon_driver", lambda self: _dummy_driver())
    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_ensure_session", lambda self: None)
    monkeypatch.setattr(night_mode_fb.time, "sleep", lambda *_: None)

    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )

    selected = enricher._search_for_page(artist, location="", allow_anon=True)
    safe_norm = night_mode_fb._normalise_fb_url(safe.url)

    assert selected == safe_norm
    assert enricher._last_selected_candidate_context["url"] == safe_norm
    assert len(enricher._last_search_candidates) == 1
    assert enricher._last_search_candidates[0]["url"] == safe_norm


def test_all_unsafe_candidates_skip_scrape(monkeypatch):
    artist = "Dental Beats"
    unsafe = _cand("Dental Beats", "https://www.facebook.com/dentalbeats", "Dental clinic")
    ranked_for_preview = [
        {
            "candidate": unsafe,
            "score": -10,
            "breakdown": [],
            "features": {
                "category": "Dental clinic",
                "descriptor": "",
                "aria_label": "",
                "secondary_text": "",
                "category_tokens": ["Dental", "Clinic"],
                "match_level": "near",
                "music_any": False,
                "service_only": True,
            },
        }
    ]
    candidates = [unsafe]

    monkeypatch.setattr(night_mode_fb, "_harvest_candidates", lambda *args, **kwargs: candidates)
    monkeypatch.setattr(night_mode_fb, "_rank_candidates_for_preview", lambda *args, **kwargs: ranked_for_preview)
    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_get_anon_driver", lambda self: _dummy_driver())
    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_ensure_session", lambda self: None)
    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_should_allow_anonymous", lambda self, row: True)
    monkeypatch.setattr(night_mode_fb.time, "sleep", lambda *_: None)

    calls = {"count": 0}

    def _spy(self, *args, **kwargs):
        calls["count"] += 1
        return None

    monkeypatch.setattr(night_mode_fb.NightModeFacebookEnricher, "_scrape_single_fb_candidate", _spy)

    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )

    row = {"Artist Name": artist, "Location": "", "FB_Status": "seeded"}
    result = enricher.enrich_row_with_facebook_night(row.copy())

    assert calls["count"] == 0
    assert result.get("FB_Reason") == "non_music_category"
    assert enricher._last_search_reject_score == -10
    assert enricher._last_search_reject_reason == "non_music_category"

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


def test_search_card_ranking_prefers_music_over_store():
    html = _load_fixture("fb_search_cards_sofia.html")
    candidates = night_mode_fb._parse_search_candidates(html, logger=None, search_name="Sofia Ly")
    assert len(candidates) == 2

    ranked = night_mode_fb._rank_candidates_for_preview("Sofia Ly", candidates)
    assert ranked[0]["candidate"].name == "SofiaLy"

    store = next(c for c in candidates if getattr(c, "name", "") == "LY SOFIA")
    music = next(c for c in candidates if getattr(c, "name", "") == "SofiaLy")
    assert getattr(music, "music_hint", False) is True

    flags_store = night_mode_fb._candidate_category_flags(
        getattr(store, "category", ""),
        getattr(store, "aria_label", ""),
        getattr(store, "secondary_text", ""),
        descriptor=getattr(store, "descriptor", ""),
        category_tokens=getattr(store, "category_tokens", []),
    )
    assert flags_store["service_only"] is True


def test_profile_php_music_page_treated_as_page_not_profile():
    artist = "Test Artist"
    profile_page_like = _cand("Test Artist", "https://www.facebook.com/profile.php?id=123456789", "Musician/band")

    score, breakdown, features = night_mode_fb._score_fb_candidate_night(artist, profile_page_like)

    assert features["is_page"] is True
    assert features["is_profile"] is False
    assert all(tok not in breakdown for tok in ("-profile", "-profile_music"))


def test_reject_cache_skips_mismatch_candidate(monkeypatch):
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
        use_shared_session=False,
    )
    bad = _cand("DJ Virginia", "https://www.facebook.com/djvirginia", "Musician/band")
    good = _cand("Willow Heights", "https://www.facebook.com/willowheightsmusic", "Musician/band")
    ranked = [
        {"candidate": bad, "score": 90, "breakdown": [], "features": {"match_level": "mismatch"}},
        {"candidate": good, "score": 80, "breakdown": [], "features": {"match_level": "near"}},
    ]
    enricher._fb_mark_rejected("Willow Heights", bad.url, "email_override_reject:name_mismatch")

    chosen, selected_by = enricher._choose_ranked_candidate("Willow Heights", ranked)
    assert chosen is good
    assert selected_by in {"ranked_sort", "mismatch_fallback"}

    # With only the rejected mismatch candidate left, selection should fail.
    solo_ranked = [{"candidate": bad, "score": 70, "breakdown": [], "features": {"match_level": "mismatch"}}]
    chosen_none, reason = enricher._choose_ranked_candidate("Another Artist", solo_ranked)
    assert chosen_none is None
    assert reason in {"no_viable_candidate", "no_safe_match"}
