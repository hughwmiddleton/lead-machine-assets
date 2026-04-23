import fb_attribution
import night_mode_fb
import pipeline_runner


def test_explicit_fb_entrypoint_urls_use_canonical_field_only():
    row = {
        "Artist Name": "Authority Gap",
        "Facebook_URL": "",
        "Social Link": "https://www.facebook.com/artistfromsocial",
        "External Links": "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr",
    }

    assert night_mode_fb.explicit_fb_entrypoint_urls_for_row(row) == []


def test_fb_opportunity_state_ignores_source_field_fallback():
    row = {
        "Artist Name": "Authority Gap",
        "Facebook_URL": "",
        "Social Link": "https://www.facebook.com/artistfromsocial",
    }

    assert fb_attribution.classify_fb_opportunity_state(row) == "no_fb_opportunity"


def test_pipeline_runner_blocks_canonical_overwrite_from_blank_or_share():
    current = "https://www.facebook.com/canonicalartist"

    assert (
        pipeline_runner._guard_authoritative_fb_write(
            current,
            "",
            artist_label="Canonical Artist",
            logger=None,
            context="test_blank",
        )
        == current
    )
    assert (
        pipeline_runner._guard_authoritative_fb_write(
            current,
            "https://www.facebook.com/share/19bactwuev?mibextid=wwXIfr",
            artist_label="Canonical Artist",
            logger=None,
            context="test_share",
        )
        == current
    )
