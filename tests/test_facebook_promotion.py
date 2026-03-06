import pytest

from source_scheduler import extract_facebook_url_from_text, promote_facebook_url


def test_extract_facebook_url_normalizes_and_filters():
    text = "https://facebook.com/artist | https://instagram.com/artist"
    assert extract_facebook_url_from_text(text) == "https://www.facebook.com/artist"


def test_promote_sets_first_valid_and_preserves_existing():
    row = {"Social Link": "https://facebook.com/artist\nhttps://instagram.com/artist", "facebook_url": ""}
    promote_facebook_url(row)
    assert row["facebook_url"] == "https://www.facebook.com/artist"

    # Existing value should not be overwritten.
    row_existing = {"facebook_url": "https://www.facebook.com/existing", "External Links": "https://facebook.com/new"}
    promote_facebook_url(row_existing)
    assert row_existing["facebook_url"] == "https://www.facebook.com/existing"


def test_promote_ignores_non_facebook_and_share_links():
    row = {"External Links": "https://instagram.com/artist"}
    promote_facebook_url(row)
    assert "facebook_url" not in row or row.get("facebook_url", "") == ""

    bad = {"Website": "https://www.facebook.com/share.php?u=abc"}
    promote_facebook_url(bad)
    assert "facebook_url" not in bad or bad.get("facebook_url", "") == ""


def test_extract_accepts_numeric_profile_and_rejects_groups():
    assert extract_facebook_url_from_text("https://www.facebook.com/profile.php?id=12345") == "https://www.facebook.com/profile.php?id=12345"
    assert extract_facebook_url_from_text("https://www.facebook.com/profile.php?id=abc") is None
    assert extract_facebook_url_from_text("https://www.facebook.com/groups/mygroup") is None


def test_promote_accepts_fb_short_domains_and_profile_ids():
    row_short = {"Social Link": "https://fb.com/exampleband", "facebook_url": ""}
    promote_facebook_url(row_short)
    assert row_short["facebook_url"] == "https://www.facebook.com/exampleband"

    row_short_me = {"External Links": "https://fb.me/exampleband"}
    promote_facebook_url(row_short_me)
    assert row_short_me["facebook_url"] == "https://www.facebook.com/exampleband"

    row_profile = {"Website": "https://www.facebook.com/profile.php?id=123456789"}
    promote_facebook_url(row_profile)
    assert row_profile["facebook_url"] == "https://www.facebook.com/profile.php?id=123456789"
