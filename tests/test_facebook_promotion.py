import pytest

import pipeline_runner
from source_scheduler import canonicalize_facebook_url, ensure_canonical_facebook_url, extract_facebook_url_from_text, promote_facebook_url


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


def test_promote_resolves_share_url_from_social_link_into_canonical_fields():
    row = {
        "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr | https://instagram.com/artist | https://youtube.com/@artist",
        "facebook_url": "",
        "Facebook_URL": "",
        "Facebook URL": "",
    }
    calls = []

    def fake_share_resolver(raw: str) -> str:
        calls.append(raw)
        return "https://m.facebook.com/artistpage/?mibextid=wwXIfr"

    promote_facebook_url(row, share_resolver=fake_share_resolver)

    assert calls == ["https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr"]
    assert row["facebook_url"] == "https://www.facebook.com/artistpage"
    assert row["Facebook_URL"] == "https://www.facebook.com/artistpage"
    assert row["Facebook URL"] == "https://www.facebook.com/artistpage"
    assert "instagram.com/artist" in row["Social Link"]
    assert "youtube.com/@artist" in row["Social Link"]


def test_promote_does_not_emit_canonical_fb_for_unresolved_share_noise():
    row = {
        "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr | https://instagram.com/artist | https://youtube.com/@artist"
    }

    promote_facebook_url(row, share_resolver=lambda raw: "https://www.facebook.com/share/stillwrapped")

    assert "facebook_url" not in row or row.get("facebook_url", "") == ""
    assert "Facebook_URL" not in row or row.get("Facebook_URL", "") == ""


def test_promote_does_not_accept_disallowed_shape_from_share_resolution():
    row = {
        "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
        "facebook_url": "",
        "Facebook_URL": "",
        "Facebook URL": "",
    }

    promote_facebook_url(row, share_resolver=lambda raw: "https://www.facebook.com/groups/not-a-page")

    assert row["facebook_url"] == ""
    assert row["Facebook_URL"] == ""
    assert row["Facebook URL"] == ""


def test_extract_accepts_numeric_profile_and_rejects_groups():
    assert extract_facebook_url_from_text("https://www.facebook.com/profile.php?id=12345") == "https://www.facebook.com/profile.php?id=12345"
    assert extract_facebook_url_from_text("https://www.facebook.com/profile.php?id=abc") is None
    assert extract_facebook_url_from_text("https://www.facebook.com/groups/mygroup") is None
    assert extract_facebook_url_from_text("https://www.facebook.com/nan") is None


def test_canonicalize_facebook_url_collapses_equivalent_mobile_and_http_variants():
    assert canonicalize_facebook_url("https://www.facebook.com/artist") == "https://www.facebook.com/artist"
    assert canonicalize_facebook_url("http://www.facebook.com/artist/") == "https://www.facebook.com/artist"
    assert canonicalize_facebook_url("http://m.facebook.com/artist") == "https://www.facebook.com/artist"


def test_direct_facebook_url_accepts_web_host_as_canonical():
    row = {"Facebook_URL": "https://web.facebook.com/foo"}

    canonical, source = ensure_canonical_facebook_url(row, set_row=False)

    assert canonical == "https://www.facebook.com/foo"
    assert source == "Facebook_URL"


def test_promote_facebook_url_accepts_web_host_from_social_link():
    row = {"Social Link": "https://web.facebook.com/foo", "facebook_url": "", "Facebook_URL": ""}

    promote_facebook_url(row)

    assert row["facebook_url"] == "https://www.facebook.com/foo"
    assert row["Facebook_URL"] == "https://www.facebook.com/foo"


def test_canonicalize_facebook_url_rejects_share_wrappers_predictably():
    assert canonicalize_facebook_url("https://www.facebook.com/share.php?u=test") == ""
    assert canonicalize_facebook_url("https://www.facebook.com/share/r/test") == ""
    assert canonicalize_facebook_url("https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr") == ""


def test_canonicalize_facebook_url_rejects_non_facebook_hosts():
    assert canonicalize_facebook_url("https://example.com/foo") == ""


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


def test_dataframe_promotion_resolves_share_url_from_social_link():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Share Link",
                "facebook_url": "",
                "Facebook_URL": "",
                "Facebook URL": "",
                "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr, https://www.instagram.com/artist, https://www.youtube.com/@artist",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")

    promoted = pipeline_runner._promote_fb_urls_df(
        df.copy(),
        share_resolver=lambda raw: "https://web.facebook.com/artistsharepage/?mibextid=wwXIfr",
    )

    assert promoted.at[0, "facebook_url"] == "https://www.facebook.com/artistsharepage"
    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/artistsharepage"
    assert promoted.at[0, "Facebook URL"] == "https://www.facebook.com/artistsharepage"


def test_dataframe_promotion_canonicalizes_share_p_shape_into_explicit_safe_profile():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Share P Link",
                "facebook_url": "",
                "Facebook_URL": "",
                "Facebook URL": "",
                "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")

    promoted = pipeline_runner._promote_fb_urls_df(
        df.copy(),
        share_resolver=lambda raw: "https://www.facebook.com/p/EchoStar-1000000000123/posts",
    )

    assert promoted.at[0, "facebook_url"] == "https://www.facebook.com/people/echostar/1000000000123"
    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/people/echostar/1000000000123"
    assert promoted.at[0, "Facebook URL"] == "https://www.facebook.com/people/echostar/1000000000123"


def test_dataframe_promotion_replaces_direct_share_alias_with_canonical_url_and_logs():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Alias Share",
                "facebook_url": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
                "Facebook_URL": "",
                "Facebook URL": "",
                "Social Link": "",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")
    logs = []

    promoted = pipeline_runner._promote_fb_urls_df(
        df.copy(),
        logger=logs.append,
        share_resolver=lambda raw: "https://web.facebook.com/aliassharepage/?mibextid=wwXIfr",
    )

    assert promoted.at[0, "facebook_url"] == "https://www.facebook.com/aliassharepage"
    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/aliassharepage"
    assert promoted.at[0, "Facebook URL"] == "https://www.facebook.com/aliassharepage"
    assert any("[FB Share Canonicalize]" in msg and "detected=1" in msg and "source_field='facebook_url'" in msg for msg in logs)
    assert any("[FB Share Canonicalize]" in msg and "outcome='resolved'" in msg for msg in logs)


def test_dataframe_promotion_preserves_unresolved_share_aliases_and_source_fields():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Unresolved Share",
                "facebook_url": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
                "Facebook_URL": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
                "Facebook URL": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
                "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr | https://www.instagram.com/unresolvedshare",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")
    logs = []

    promoted = pipeline_runner._promote_fb_urls_df(
        df.copy(),
        logger=logs.append,
        share_resolver=lambda raw: "https://www.facebook.com/share/stillwrapped",
    )

    assert promoted.at[0, "facebook_url"] == "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr"
    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr"
    assert promoted.at[0, "Facebook URL"] == "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr"
    assert "instagram.com/unresolvedshare" in promoted.at[0, "Social Link"]
    assert "facebook.com/share/19BActwuev" in promoted.at[0, "Social Link"]
    assert any("[FB Share Canonicalize]" in msg and "detected=1" in msg and "source_field='Facebook_URL'" in msg for msg in logs)
    assert any(
        "[FB Share Canonicalize]" in msg
        and "outcome='unresolved'" in msg
        and "reason='redirect_stayed_on_share_wrapper'" in msg
        for msg in logs
    )
    assert any(
        "[FB Share Intake Trace]" in msg
        and "raw_fb_url='https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr'" in msg
        and "resolution_attempted=true" in msg
        and "resolution_success=false" in msg
        and "state='share_runtime_fallback'" in msg
        for msg in logs
    )


def test_share_in_social_link_is_resolved_before_opportunity_detection():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Social Share",
                "Social Link": "https://www.instagram.com/social | https://www.facebook.com/share/XYZ",
                "External Links": "",
                "Facebook_URL": "",
                "Facebook URL": "",
                "facebook_url": "",
                pipeline_runner.FB_OPPORTUNITY_STATE_COL: "",
                pipeline_runner.FB_GATE_STATE_COL: "",
            }
        ],
        dtype=str,
    ).fillna("")
    logs = []
    calls = []

    pipeline_runner._promote_fb_urls_df(
        df,
        logger=logs.append,
        share_resolver=lambda raw: (calls.append(raw) or "https://www.facebook.com/socialcanonical"),
    )
    pipeline_runner.apply_fb_opportunity_state_df(df, overwrite=False)

    assert calls == ["https://www.facebook.com/share/XYZ"]
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/socialcanonical"
    assert df.at[0, pipeline_runner.FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"
    assert any(
        "[FB Share Intake Trace]" in msg
        and "resolved_fb_url='https://www.facebook.com/socialcanonical'" in msg
        and "resolution_success=true" in msg
        for msg in logs
    )


def test_share_in_external_links_is_resolved_before_opportunity_detection():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "External Share",
                "Social Link": "",
                "External Links": "https://open.spotify.com/artist/1 | https://www.facebook.com/share/XYZ",
                "Facebook_URL": "",
                "Facebook URL": "",
                "facebook_url": "",
                pipeline_runner.FB_OPPORTUNITY_STATE_COL: "",
                pipeline_runner.FB_GATE_STATE_COL: "",
            }
        ],
        dtype=str,
    ).fillna("")

    pipeline_runner._promote_fb_urls_df(
        df,
        share_resolver=lambda raw: "https://www.facebook.com/externalcanonical",
    )
    pipeline_runner.apply_fb_opportunity_state_df(df, overwrite=False)

    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/externalcanonical"
    assert df.at[0, pipeline_runner.FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"


def test_canonical_fb_url_bypasses_share_resolver_and_keeps_existing_behaviour():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Canonical Artist",
                "Social Link": "https://www.facebook.com/some.artist",
                "External Links": "",
                "Facebook_URL": "",
                "Facebook URL": "",
                "facebook_url": "",
                pipeline_runner.FB_OPPORTUNITY_STATE_COL: "",
                pipeline_runner.FB_GATE_STATE_COL: "",
            }
        ],
        dtype=str,
    ).fillna("")

    def fail(raw: str) -> str:
        raise AssertionError("resolver should not be called for canonical Facebook URLs")

    pipeline_runner._promote_fb_urls_df(df, share_resolver=fail)
    pipeline_runner.apply_fb_opportunity_state_df(df, overwrite=False)

    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/some.artist"
    assert df.at[0, pipeline_runner.FB_OPPORTUNITY_STATE_COL] == "fb_opportunity_present"


def test_share_resolution_failure_seeds_runtime_fallback_before_no_opportunity():
    raw_share = "https://www.facebook.com/share/XYZ"
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Failed Share",
                "Social Link": raw_share,
                "External Links": "",
                "Facebook_URL": "",
                "Facebook URL": "",
                "facebook_url": "",
                pipeline_runner.FB_OPPORTUNITY_STATE_COL: "",
                pipeline_runner.FB_GATE_STATE_COL: "",
            }
        ],
        dtype=str,
    ).fillna("")
    logs = []

    pipeline_runner._promote_fb_urls_df(df, logger=logs.append, share_resolver=lambda raw: "")
    pipeline_runner.apply_fb_opportunity_state_df(df, overwrite=False)

    assert raw_share in df.at[0, "Social Link"]
    assert df.at[0, "Facebook_URL"] == ""
    assert df.at[0, pipeline_runner.FB_SHARE_RUNTIME_FALLBACK_URL_COL] == raw_share
    assert df.at[0, pipeline_runner.FB_SHARE_RUNTIME_FALLBACK_SOURCE_COL] == "Social Link"
    assert df.at[0, pipeline_runner.FB_GATE_STATE_COL] == ""
    assert df.at[0, pipeline_runner.FB_OPPORTUNITY_STATE_COL] == "no_fb_opportunity"
    assert any("resolution_attempted=true" in msg and "resolution_success=false" in msg for msg in logs)
    assert any("state='share_runtime_fallback'" in msg for msg in logs)


def test_unresolved_share_does_not_gate_before_pass_a():
    raw_share = "https://www.facebook.com/share/XYZ"
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "PASS A Share",
                "Source Directory": "Unearthed",
                "Social Link": raw_share,
                "External Links": "",
                "Facebook_URL": "",
                "Facebook URL": "",
                "facebook_url": "",
                pipeline_runner.FB_GATE_STATE_COL: "",
            }
        ],
        dtype=str,
    ).fillna("")

    pipeline_runner._promote_fb_urls_df(df, share_resolver=lambda raw: "")
    row = df.loc[0].to_dict()

    assert df.at[0, pipeline_runner.FB_GATE_STATE_COL] != "fb_share_resolution_failed"
    assert pipeline_runner.explicit_fb_entrypoint_urls_for_row(row) == []
    assert pipeline_runner.fb_share_runtime_fallback_urls_for_row(row) == [raw_share]
    assert pipeline_runner.explicit_fb_entrypoint_present_for_row(row) is True


def test_attempted_share_runtime_fallback_is_not_reentered():
    raw_share = "https://www.facebook.com/share/XYZ"
    row = {
        pipeline_runner.FB_SHARE_RUNTIME_FALLBACK_URL_COL: raw_share,
        pipeline_runner.FB_SHARE_RUNTIME_FALLBACK_SOURCE_COL: "Social Link",
        pipeline_runner.FB_SHARE_RUNTIME_FALLBACK_ATTEMPTED_COL: "1",
    }

    assert pipeline_runner.fb_share_runtime_fallback_urls_for_row(row) == []
    assert pipeline_runner.explicit_fb_entrypoint_present_for_row(row) is False


def test_dataframe_promotion_preserves_existing_canonical_without_invoking_share_resolver():
    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Canonical Wins",
                "facebook_url": "",
                "Facebook_URL": "https://www.facebook.com/alreadycanonical",
                "Facebook URL": "",
                "Social Link": "https://www.facebook.com/share/19BActwuev?mibextid=wwXIfr",
                "External Links": "",
            }
        ],
        dtype=str,
    ).fillna("")
    calls = []

    promoted = pipeline_runner._promote_fb_urls_df(
        df.copy(),
        share_resolver=lambda raw: (calls.append(raw) or "https://www.facebook.com/should-not-win"),
    )

    assert calls == []
    assert promoted.at[0, "Facebook_URL"] == "https://www.facebook.com/alreadycanonical"
    assert promoted.at[0, "facebook_url"] == "https://www.facebook.com/alreadycanonical"
    assert promoted.at[0, "Facebook URL"] == "https://www.facebook.com/alreadycanonical"


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


def test_payload_promotion_backfills_canonical_facebook_url():
    pytest.importorskip("PyQt5")
    import cross_directory_enricher as cde

    df = pytest.importorskip("pandas").DataFrame(
        [{"Artist Name": "Payload FB", "Facebook_URL": "", "facebook_url": "", "Facebook URL": ""}],
        dtype=str,
    ).fillna("")
    payload = cde.EnrichmentPayload(
        socials={"https://fb.com/payloadband"},
        websites=set(),
        source_dir="soundcloud",
        source_url="https://soundcloud.com/payloadband",
        match_score=0.9,
    )

    promoted = cde._promote_payload_facebook_url(df, 0, payload)

    assert promoted is True
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/payloadband"


def test_payload_promotion_preserves_same_page_existing_canonical_without_churn():
    pytest.importorskip("PyQt5")
    import cross_directory_enricher as cde

    df = pytest.importorskip("pandas").DataFrame(
        [
            {
                "Artist Name": "Existing Canonical",
                "Facebook_URL": "https://www.facebook.com/existingband",
                "facebook_url": "",
                "Facebook URL": "",
            }
        ],
        dtype=str,
    ).fillna("")
    payload = cde.EnrichmentPayload(
        socials={"https://fb.com/existingband", "https://www.facebook.com/existingband/"},
        websites=set(),
        source_dir="lastfm",
        source_url="https://www.last.fm/music/Existing+Band",
        match_score=0.9,
    )

    promoted = cde._promote_payload_facebook_url(df, 0, payload)

    assert promoted is True
    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/existingband"


def test_payload_promotion_skips_conflicting_facebook_pages():
    pytest.importorskip("PyQt5")
    import cross_directory_enricher as cde

    df = pytest.importorskip("pandas").DataFrame(
        [{"Artist Name": "Conflicting Payload", "Facebook_URL": "", "facebook_url": "", "Facebook URL": ""}],
        dtype=str,
    ).fillna("")
    payload = cde.EnrichmentPayload(
        socials={"https://www.facebook.com/pageone", "https://www.facebook.com/pagetwo"},
        websites=set(),
        source_dir="bandcamp",
        source_url="https://conflicting.bandcamp.com",
        match_score=0.95,
    )

    promoted = cde._promote_payload_facebook_url(df, 0, payload)

    assert promoted is False
    assert df.at[0, "Facebook_URL"] == ""
