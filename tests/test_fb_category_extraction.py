from pathlib import Path

from bs4 import BeautifulSoup

import facebook_enrich


def _extract_from_html(html: str, page_name: str = ""):
    soup = BeautifulSoup(html, "html.parser")
    anchor = soup.find("a")
    assert anchor is not None
    return facebook_enrich._extract_fb_category_candidates(anchor, page_name=page_name)


def test_aria_label_music_and_descriptor():
    html = '<div><a aria-label="Name · Musician/Band · Public figure">Name</a></div>'
    primary, descriptor, tokens = _extract_from_html(html, page_name="Name")
    assert primary == "Musician/Band"
    assert descriptor == "Public figure"
    assert "Musician/Band" in tokens
    assert "Public figure" in tokens


def test_product_service_descriptor_kept():
    html = '<div><a aria-label="Shelailai · Product/service · Clothing (brand)">Shelailai</a></div>'
    primary, descriptor, tokens = _extract_from_html(html, page_name="Shelailai")
    assert primary == "Product/service"
    assert descriptor == "Clothing (brand)"
    assert "Product/service" in tokens
    assert "Clothing (brand)" in tokens


def test_followers_and_reviews_ignored():
    html = '<div><a aria-label="DJ Test · Musician/Band · 12K followers">DJ Test</a></div>'
    primary, descriptor, tokens = _extract_from_html(html, page_name="DJ Test")
    assert primary == "Musician/Band"
    assert descriptor in (None, "", "Musician/Band")
    assert all("followers" not in t.lower() for t in tokens)


def test_token_length_cap_applied_per_token():
    long_token = "x" * 90
    html = f'<div><a aria-label="Name · Musician/Band · {long_token}">Name</a></div>'
    primary, descriptor, tokens = _extract_from_html(html, page_name="Name")
    assert primary == "Musician/Band"
    assert long_token not in tokens


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_search_card_subtitle_extraction():
    html = _load_fixture("fb_search_cards_sofia.html")
    soup = BeautifulSoup(html, "html.parser")
    musician_anchor = soup.find("a", string="SofiaLy")
    store_anchor = soup.find("a", string="LY SOFIA")

    primary_music, descriptor_music, tokens_music = facebook_enrich._extract_fb_category_candidates(musician_anchor, page_name="SofiaLy")
    assert primary_music == "Musician/band"
    assert descriptor_music == "Musician/band"
    assert "Musician/band" in tokens_music

    primary_store, descriptor_store, tokens_store = facebook_enrich._extract_fb_category_candidates(store_anchor, page_name="LY SOFIA")
    assert primary_store == "Furniture store"
    assert descriptor_store == "Furniture store"
    assert "Furniture store" in tokens_store
