from fb_email_override import should_accept_email_override


def test_reject_business_furniture_store_with_email_near_match() -> None:
    accept, reason = should_accept_email_override(
        "Sofia Ly",
        {"name": "LY SOFIA", "category": "Furniture store", "base_score": 2.0},
        {"music_hint": False, "score": 2.0},
    )
    assert accept is False
    assert reason == "email_override_reject:name_mismatch"


def test_reject_digital_creator() -> None:
    accept, reason = should_accept_email_override(
        "Runway the Catwalker",
        {"name": "Career Catwalkers", "category": "Digital creator", "base_score": 1.5},
        {"music_hint": False, "score": 1.5},
    )
    assert accept is False
    assert reason == "email_override_reject:name_mismatch"


def test_accept_musician_band_category() -> None:
    accept, reason = should_accept_email_override(
        "Some Band",
        {"name": "Some Band", "category": "Musician/Band", "base_score": 1.2},
        {"music_hint": False, "score": 1.2},
    )
    assert accept is True
    assert reason == "email_override_accept:music_hint"


def test_accept_music_hint_flag() -> None:
    accept, reason = should_accept_email_override(
        "DJ Test",
        {"name": "DJ Test", "category": "Creator", "base_score": 0.5},
        {"music_hint": True, "score": 0.5},
    )
    assert accept is True
    assert reason == "email_override_accept:music_hint"


def test_accept_identity_softpass() -> None:
    accept, reason = should_accept_email_override(
        "Exact Artist",
        {"name": "Exact Artist", "category": "", "base_score": 1.1},
        {"music_hint": False, "score": 1.1, "descriptor": "Exact Artist band"},
    )
    assert accept is True
    assert reason == "email_override_accept:music_hint"


def test_reject_name_mismatch() -> None:
    accept, reason = should_accept_email_override(
        "Original Artist",
        {"name": "Different Name", "category": "Musician/Band", "base_score": 2.0},
        {"music_hint": False, "score": 2.0},
    )
    assert accept is False
    assert reason == "email_override_reject:name_mismatch"


def test_reject_non_music_verified_page_with_email() -> None:
    accept, reason = should_accept_email_override(
        "Lewis Pugh",
        {"name": "Lewis Pugh Oceans", "category": "Public figure", "base_score": 0.5},
        {"music_hint": False, "score": 0.5, "descriptor": "Lewis Pugh Oceans verified"},
    )
    assert accept is False
    assert reason == "email_override_reject:weak_nonmusic_match"


def test_accept_identity_with_seed_url_and_no_music_hint() -> None:
    accept, reason = should_accept_email_override(
        "Exact Artist",
        {"name": "Exact Artist", "category": "Public figure", "base_score": 1.3},
        {"music_hint": False, "score": 1.3, "seed_url_match": True},
    )
    assert accept is True
    assert reason == "email_override_accept:identity_softpass"
