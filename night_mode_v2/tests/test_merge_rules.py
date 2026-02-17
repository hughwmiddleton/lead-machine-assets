from night_mode_v2.merge_rules import apply_merge


def test_fill_blank_does_not_overwrite():
    target = {"Primary Email": "a@example.com"}
    source = {"Primary Email": "b@example.com"}

    merged = apply_merge(target, source)
    assert merged["Primary Email"] == "a@example.com"


def test_union_multi_deduplicates():
    target = {"All Emails": "a@example.com; b@example.com"}
    source = {"All Emails": "b@example.com; c@example.com"}

    merged = apply_merge(target, source)
    assert merged["All Emails"] == "a@example.com;b@example.com;c@example.com"


def test_status_worst_wins():
    target = {"Status": "OK"}
    source = {"Status": "BLOCK"}

    merged = apply_merge(target, source)
    assert merged["Status"] == "BLOCK"


def test_max_wins_keeps_larger_number():
    target = {"Playcount": 3}
    source = {"Playcount": 5}

    merged = apply_merge(target, source)
    assert merged["Playcount"] == 5
