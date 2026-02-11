from fb_email_override import should_accept_email_override


def test_reject_business_furniture_store_with_email_near_match() -> None:
    accept, reason = should_accept_email_override(
        "Sofia Ly",
        {"name": "LY SOFIA", "category": "Furniture store", "base_score": 2.0},
        {"music_hint": False, "score": 2.0},
    )
    assert accept is False
    assert reason == "email_override_reject:service_category"


def test_reject_digital_creator() -> None:
    accept, reason = should_accept_email_override(
        "Runway the Catwalker",
        {"name": "Career Catwalkers", "category": "Digital creator", "base_score": 1.5},
        {"music_hint": False, "score": 1.5},
    )
    assert accept is False
    assert reason == "email_override_reject:service_category"


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
        {"music_hint": False, "score": 1.1},
    )
    assert accept is True
    assert reason == "email_override_accept:identity_softpass"


def test_reject_name_mismatch() -> None:
    accept, reason = should_accept_email_override(
        "Original Artist",
        {"name": "Different Name", "category": "Musician/Band", "base_score": 2.0},
        {"music_hint": False, "score": 2.0},
    )
    assert accept is False
    assert reason == "email_override_reject:name_mismatch"
