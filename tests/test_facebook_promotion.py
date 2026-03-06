import pytest

import pipeline_runner
from source_scheduler import extract_facebook_url_from_text, promote_facebook_url


def test_extract_facebook_url_normalizes_and_filters():
    text = "https://facebook.com/artist | https://instagram.com/artist"
    assert extract_facebook_url_from_text(text) == "https://www.facebook.com/artist"


def test_promote_sets_first_valid_and_preserves_existing():
    row = {"Social Link": "https://facebook.com/artist\nhttps://instagram.com/artist", "facebook_url": ""}
    promote_facebook_url(row)
    assert row["facebook_url"] == "https://www.facebook.com/artist"
    assert row["Facebook_URL"] == "https://www.facebook.com/artist"

    # Existing value should not be overwritten.
    row_existing = {
        "Facebook_URL": "https://www.facebook.com/existing",
        "facebook_url": "",
        "External Links": "https://facebook.com/new",
    }
    promote_facebook_url(row_existing)
    assert row_existing["Facebook_URL"] == "https://www.facebook.com/existing"
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
    assert extract_facebook_url_from_text("https://www.facebook.com/nan") is None


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
    assert row_profile["Facebook_URL"] == "https://www.facebook.com/profile.php?id=123456789"


def test_dataframe_promotion_backfills_canonical_from_lower_alias():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Lower Alias",
                "facebook_url": "https://www.facebook.com/existinglower",
                "Facebook_URL": "",
                "Social Link": "",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")

    promoted = pipeline_runner._promote_fb_urls_df(df.copy())

    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/existinglower"


def test_dataframe_promotion_extracts_canonical_from_mixed_social_link():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Mixed Social",
                "facebook_url": "",
                "Facebook_URL": "",
                "Social Link": "https://www.facebook.com/georgerileymusic, https://www.instagram.com/georgeriley___",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")

    promoted = pipeline_runner._promote_fb_urls_df(df.copy())

    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/georgerileymusic"


def test_dataframe_promotion_preserves_existing_canonical_value():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Canonical Wins",
                "facebook_url": "https://www.facebook.com/differentalias",
                "Facebook_URL": "https://www.facebook.com/alreadycanonical",
                "Social Link": "",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")

    promoted = pipeline_runner._promote_fb_urls_df(df.copy())

    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/alreadycanonical"


def test_dataframe_promotion_preserves_profile_php_urls():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Profile ID",
                "facebook_url": "https://www.facebook.com/profile.php?id=123&foo=1",
                "Facebook_URL": "",
                "Social Link": "",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")

    promoted = pipeline_runner._promote_fb_urls_df(df.copy())

    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/profile.php?id=123"
