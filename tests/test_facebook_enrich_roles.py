import facebook_enrich


def test_is_musician_page_english_labels() -> None:
    assert facebook_enrich.is_musician_page("Musician/band", None)
    assert facebook_enrich.is_musician_page("Musician", None)
    assert facebook_enrich.is_musician_page(None, "Artist")


def test_is_musician_page_spanish_portuguese_labels() -> None:
    assert facebook_enrich.is_musician_page("Músico/banda", None)
    assert facebook_enrich.is_musician_page("Musico", None)
    assert facebook_enrich.is_musician_page(None, "Banda")


def test_is_musician_page_french_italian_labels() -> None:
    assert facebook_enrich.is_musician_page("Musicien", None)
    assert facebook_enrich.is_musician_page(None, "Groupe musical")
    assert facebook_enrich.is_musician_page("Musicista/band", None)
    assert facebook_enrich.is_musician_page(None, "Gruppo musicale")


def test_is_musician_page_negative_labels() -> None:
    assert not facebook_enrich.is_musician_page("Restaurant", None)
    assert not facebook_enrich.is_musician_page(None, "Company")
    assert not facebook_enrich.is_musician_page("Local business", "Blogger")


def test_normalize_role_text_strips_accents_and_whitespace() -> None:
    assert facebook_enrich.normalize_role_text("  Músico/banda  ") == "musico/banda"
    assert facebook_enrich.normalize_role_text("Musicien · Groupe") == "musicien · groupe"
