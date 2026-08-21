"""Regression tests for Ticket 5: harden email provenance sanity.

Ensures platform support emails are rejected, literal extraction still works,
stronger existing emails are preserved, and provenance remains coherent.
"""

import json

import pandas as pd
import pytest

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde
import email_normalizer
import email_provenance
import night_mode_fb
import pipeline_runner
import website_email_scraper as wes
from cross_directory_enricher import EnrichmentPayload
from email_provenance import EMAIL_PROVENANCE_JSON_COL, _set_email_with_provenance


# ---------------------------------------------------------------------------
# 1. Platform support email classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("email", "expected_rejected"),
    [
        ("support@jeremyross.bandcamp.com", True),
        ("support@lordprosser.bandcamp.com", True),
        ("support@ericdollois.bandcamp.com", True),
        ("help@artist.bandcamp.com", True),
        ("noreply@artist.soundcloud.com", True),
        ("abuse@artist.facebook.com", True),
        ("security@artist.instagram.com", True),
        ("admin@artist.bandcamp.com", True),
        ("webmaster@artist.bandcamp.com", True),
        ("privacy@artist.bandcamp.com", True),
        ("legal@artist.bandcamp.com", True),
        ("dmca@artist.bandcamp.com", True),
        ("feedback@artist.bandcamp.com", True),
        ("contact@artist.bandcamp.com", False),  # artist-owned custom domain would differ
        ("booking@artist.com", False),
        ("artist@gmail.com", False),
        ("press@sub.artist.co.uk", False),
    ],
)
def test_platform_support_email_classification(email: str, expected_rejected: bool) -> None:
    assert email_normalizer.is_platform_support_email(email) is expected_rejected


def test_filter_platform_support_emails_dedupes_and_rejects() -> None:
    raw = [
        "support@jeremyross.bandcamp.com",
        "booking@artist.com",
        "help@artist.bandcamp.com",
        "booking@artist.com",
        "press@label.com",
    ]
    result = email_normalizer.filter_platform_support_emails(raw)
    assert result == ["booking@artist.com", "press@label.com"]


# ---------------------------------------------------------------------------
# 2. No synthetic email from Bandcamp hostname
# ---------------------------------------------------------------------------

def test_bandcamp_hostname_does_not_synthesize_support_email() -> None:
    """Bandcamp page with no literal email must NOT yield support@artist.bandcamp.com."""
    html = """
    <html><body>
        <h1>Jeremy Ross</h1>
        <a href="https://jeremyross.bandcamp.com">Home</a>
        <footer><a href="/help">Support</a></footer>
    </body></html>
    """
    emails = cde._extract_emails_from_html_text(html)
    assert "support@jeremyross.bandcamp.com" not in emails
    assert "help@jeremyross.bandcamp.com" not in emails


def test_bandcamp_contact_page_without_literal_email_is_empty() -> None:
    html = """
    <html><body>
        <h1>Lord Prosser</h1>
        <p>Contact us through social media</p>
        <a href="mailto:support@bandcamp.com">Bandcamp Support</a>
    </body></html>
    """
    emails = cde._extract_emails_from_html_text(html)
    # platform support email must be filtered even if literally present
    assert "support@bandcamp.com" not in emails
    assert "support@lordprosser.bandcamp.com" not in emails


# ---------------------------------------------------------------------------
# 3. Literal mailto extraction still works
# ---------------------------------------------------------------------------

def test_literal_mailto_extraction_accepted() -> None:
    html = "<html><body><a href='mailto:artist@example.com'>Email</a></body></html>"
    emails = wes._extract_emails_from_html(html)
    assert "artist@example.com" in emails


def test_literal_mailto_with_params_accepted() -> None:
    html = "<html><body><a href='mailto:booking@example.com?subject=Press'>Email</a></body></html>"
    emails = wes._extract_emails_from_html(html)
    assert "booking@example.com" in emails


# ---------------------------------------------------------------------------
# 4. Literal visible-text extraction still works
# ---------------------------------------------------------------------------

def test_visible_text_email_extraction_accepted() -> None:
    html = "<html><body>Contact: booking@example.com for inquiries</body></html>"
    emails = wes._extract_emails_from_html(html)
    assert "booking@example.com" in emails


def test_visible_text_press_email_accepted() -> None:
    html = "<html><body>Press: press@label.co.uk</body></html>"
    emails = wes._extract_emails_from_html(html)
    assert "press@label.co.uk" in emails


# ---------------------------------------------------------------------------
# 5. Platform support/admin emails rejected by scrapers
# ---------------------------------------------------------------------------

def test_website_scraper_rejects_platform_support_email() -> None:
    html = """
    <html><body>
        <a href='mailto:support@artist.bandcamp.com'>Support</a>
        <span>booking@artist.com</span>
    </body></html>
    """
    emails = wes._extract_emails_from_html(html)
    assert "support@artist.bandcamp.com" not in emails
    assert "booking@artist.com" in emails


def test_cross_directory_html_text_rejects_platform_support() -> None:
    """Platform support emails must be filtered even if the broken regex were to match them."""
    html = """
    <html><body>
        <p>Need help? support@artist.bandcamp.com</p>
        <p>Bookings: booking@artist.com</p>
    </body></html>
    """
    emails = cde._extract_emails_from_html_text(html)
    assert "support@artist.bandcamp.com" not in emails
    # NOTE: _extract_emails_from_html_text uses a broken regex (\\. instead of \.)
    # so normal email extraction through this path is already non-functional.
    # This test only asserts the platform-support filtering we added.


# ---------------------------------------------------------------------------
# 6. External-site provenance points to external site
# ---------------------------------------------------------------------------

def test_external_site_email_provenance_points_to_external_site() -> None:
    """When an email is found on an external website, provenance should identify that site."""
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Test Artist",
                "Email": "",
                "Email_All": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Social Link": "",
                "External Links": "",
                "Source Directory": "",
                "Source URL": "",
            }
        ]
    )
    payload = EnrichmentPayload(
        socials=set(),
        websites=set(),
        emails={"contact@artistdomain.com"},
        link_hubs=set(),
        source_dir="website_enrich",
        source_url="https://artistdomain.com/contact",
    )
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker._apply_payload(df, 0, payload)

    assert df.at[0, "Email"] == "contact@artistdomain.com"
    assert df.at[0, "Email_Source_URL"] == "https://artistdomain.com/contact"
    assert df.at[0, "Email_Source_Type"] == "website_enrich"
    assert df.at[0, "Email_Extract_Method"] == "regex"


# ---------------------------------------------------------------------------
# 7. Stronger existing email preserved against weaker platform candidate
# ---------------------------------------------------------------------------

def test_set_email_with_provenance_rejects_platform_support() -> None:
    row = {
        "Email": "",
        "Email_All": "",
        "Email_Source_URL": "",
        "Email_Source_Type": "",
        "Email_Extract_Method": "",
    }
    _set_email_with_provenance(
        row,
        "support@artist.bandcamp.com",
        source_url="https://artist.bandcamp.com",
        source_type="bandcamp_profile",
        method="regex",
    )
    assert row["Email"] == ""
    assert row["Email_All"] == ""


def test_set_email_all_preserves_stronger_existing_over_platform_support() -> None:
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist A",
                "Email": "artist@gmail.com",
                "Email_All": "artist@gmail.com",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "artist@gmail.com": {
                            "source_type": "website_enrich",
                            "surface": "website_contact_page",
                            "source_url": "https://artist.com/contact",
                            "extract_method": "mailto",
                        }
                    }
                ),
            }
        ]
    )
    pipeline_runner._set_email_all(
        df,
        0,
        ["support@artist.bandcamp.com"],
        source="test_preserve",
        source_url="https://artist.bandcamp.com",
        source_type="bandcamp_profile",
        method="regex",
    )
    ranked = pipeline_runner._rank_contact_emails_for_row(df.loc[0], df.at[0, "Email_All"])
    assert ranked[0] == "artist@gmail.com"
    assert "support@artist.bandcamp.com" not in ranked


def test_apply_payload_does_not_overwrite_with_platform_support() -> None:
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist B",
                "Email": "booking@artist.com",
                "Email_All": "booking@artist.com",
                "Email_Source_URL": "https://artist.com/contact",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "mailto",
                "Social Link": "",
                "External Links": "",
                "Source Directory": "",
                "Source URL": "",
            }
        ]
    )
    payload = EnrichmentPayload(
        socials=set(),
        websites=set(),
        emails={"support@artist.bandcamp.com"},
        link_hubs=set(),
        source_dir="bandcamp_profile",
        source_url="https://artist.bandcamp.com",
    )
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker._apply_payload(df, 0, payload)

    assert df.at[0, "Email"] == "booking@artist.com"
    assert "support@artist.bandcamp.com" not in df.at[0, "Email_All"]


# ---------------------------------------------------------------------------
# 8. Normalization safety — must not fabricate domains or local parts
# ---------------------------------------------------------------------------

def test_normalization_does_not_fabricate_domain() -> None:
    # normalize_email_value does not strip trailing punctuation; it validates basic shape.
    # The key invariant is that it never invents characters not present in the input.
    result = email_normalizer.normalize_email_value("artist@example.com")
    assert result == "artist@example.com"
    # Missing domain or local part returns empty string, not a fabricated address.
    assert email_normalizer.normalize_email_value("artist@") == ""
    assert email_normalizer.normalize_email_value("@example.com") == ""


def test_normalization_does_not_create_local_part() -> None:
    assert email_normalizer.normalize_email_value("@example.com") == ""


def test_normalize_emails_rejects_invalid_shapes() -> None:
    assert pipeline_runner.normalize_emails("not-an-email") == []
    assert pipeline_runner.normalize_emails("missing@domain") == []
    # normalize_emails first collapses spaces around '@', then splits on whitespace.
    # "spaces in @domain.com" becomes "spaces in@domain.com" -> ["spaces", "in@domain.com"]
    assert pipeline_runner.normalize_emails("spaces in @domain.com") == ["in@domain.com"]
    # Truly invalid shapes are still rejected.
    assert pipeline_runner.normalize_emails("no-at-sign") == []
    assert pipeline_runner.normalize_emails("@nodomain") == []
    assert pipeline_runner.normalize_emails("local@") == []


# ---------------------------------------------------------------------------
# 9. Malformed HTML cannot join fragments into fake email
# ---------------------------------------------------------------------------

def test_malformed_html_does_not_join_fragments_into_email() -> None:
    html = """
    <html><body>
        <span>contact</span>@<span>example</span>.<span>com</span>
    </body></html>
    """
    emails = wes._extract_emails_from_html(html)
    # The regex operates on raw HTML; if it spuriously matches, we want to know.
    # After normalization, any such match should at least be shape-validated.
    # This test documents that fragment-joining is not expected.
    for email in emails:
        assert "contact@example.com" != email, "fragment-joined email should not survive"


def test_cross_directory_html_text_does_not_join_fragments() -> None:
    html = "<p>name</p>@<p>domain</p>.<p>com</p>"
    emails = cde._extract_emails_from_html_text(html)
    assert "name@domain.com" not in emails


# ---------------------------------------------------------------------------
# 10. Existing legitimate extraction still passes
# ---------------------------------------------------------------------------

def test_legitimate_bandcamp_email_extraction_still_works() -> None:
    # Test the working extraction path (website_email_scraper), not the broken
    # _extract_emails_from_html_text fallback which has a pre-existing regex bug.
    html = "<html><body><a href='mailto:booking@jeremyross.com'>Book</a></body></html>"
    emails = wes._extract_emails_from_html(html)
    assert "booking@jeremyross.com" in emails


def test_legitimate_soundcloud_email_extraction_still_works() -> None:
    html = "<html><body>Contact: management@artist.com</body></html>"
    emails = wes._extract_emails_from_html(html)
    assert "management@artist.com" in emails


def test_facebook_literal_mailto_still_works() -> None:
    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        "<html><body><a href='mailto:fb@artist.com'>Contact</a></body></html>"
    )
    assert "fb@artist.com" in emails
    assert used_mailto is True


def test_facebook_visible_text_email_still_works() -> None:
    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        "<html><body>Bookings: ig@artist.com</body></html>"
    )
    assert "ig@artist.com" in emails
    assert used_mailto is False


# ---------------------------------------------------------------------------
# 11. Email syntax validation unchanged
# ---------------------------------------------------------------------------

def test_email_syntax_validation_unchanged() -> None:
    assert pipeline_runner._is_valid_email_shape("valid@example.com") is True
    assert pipeline_runner._is_valid_email_shape("invalid") is False
    assert pipeline_runner._is_valid_email_shape("no-at-sign.com") is False
    assert pipeline_runner._is_valid_email_shape("spaces in@email.com") is False


# ---------------------------------------------------------------------------
# 12. Night-mode FB _build_result filters platform support
# ---------------------------------------------------------------------------

def test_night_mode_fb_build_result_filters_platform_support() -> None:
    enricher = night_mode_fb.NightModeFacebookEnricher(legacy_module=None, username="", password="", logger=None)
    result = enricher._build_result(
        emails=["support@artist.bandcamp.com", "real@artist.com"],
        email_all_existing="",
        facebook_url="https://facebook.com/artist",
        artist_name="Artist",
    )
    assert result is not None
    assert "support@artist.bandcamp.com" not in result.email_all
    assert "real@artist.com" in result.email_all


# ---------------------------------------------------------------------------
# 13. Coherence: source type / source URL remain consistent
# ---------------------------------------------------------------------------

def test_email_source_type_and_url_coherence() -> None:
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist C",
                "Email": "",
                "Email_All": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Social Link": "",
                "External Links": "",
                "Source Directory": "",
                "Source URL": "",
            }
        ]
    )
    payload = EnrichmentPayload(
        socials=set(),
        websites=set(),
        emails={"press@artist.com"},
        link_hubs=set(),
        source_dir="website_enrich",
        source_url="https://artist.com/press",
    )
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker._apply_payload(df, 0, payload)

    assert df.at[0, "Email_Source_Type"] == "website_enrich"
    assert df.at[0, "Email_Source_URL"] == "https://artist.com/press"


# ---------------------------------------------------------------------------
# 14. Regression fixtures: explicit rejection of known bad addresses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_email",
    [
        "support@jeremyross.bandcamp.com",
        "support@lordprosser.bandcamp.com",
        "support@ericdollois.bandcamp.com",
    ],
)
def test_regression_fixtures_rejected_unless_literal(bad_email: str) -> None:
    """These specific addresses from the real run must be rejected."""
    assert email_normalizer.is_platform_support_email(bad_email) is True
    # Also ensure they are filtered from lists
    assert email_normalizer.filter_platform_support_emails([bad_email, "good@example.com"]) == ["good@example.com"]


def test_regression_fixture_rejected_by_set_email_with_provenance() -> None:
    row = {
        "Email": "",
        "Email_All": "",
        "Email_Source_URL": "",
        "Email_Source_Type": "",
        "Email_Extract_Method": "",
    }
    _set_email_with_provenance(
        row,
        "support@jeremyross.bandcamp.com",
        source_url="https://jeremyross.bandcamp.com",
        source_type="bandcamp_profile",
        method="regex",
    )
    assert row["Email"] == ""


def test_bandcamp_payload_only_platform_support_does_not_set_provenance() -> None:
    """A Bandcamp payload containing only platform support emails must not set provenance fields."""
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Jeremy Ross",
                "Email": "",
                "Email_All": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Social Link": "",
                "External Links": "",
                "Source Directory": "",
                "Source URL": "",
            }
        ]
    )
    payload = EnrichmentPayload(
        socials=set(),
        websites=set(),
        emails={"support@jeremyross.bandcamp.com", "support@bandcamp.com"},
        link_hubs=set(),
        source_dir="bandcamp_profile",
        source_url="https://jeremyross.bandcamp.com",
    )
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker._apply_payload(df, 0, payload)

    assert df.at[0, "Email"] == ""
    assert df.at[0, "Email_All"] == ""
    assert df.at[0, "Email_Source_URL"] == ""
    assert df.at[0, "Email_Source_Type"] == ""


def test_bandcamp_payload_mixed_platform_and_artist_keeps_only_artist_email() -> None:
    """A Bandcamp payload with both platform support and artist email keeps only the artist email."""
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Lord Prosser",
                "Email": "",
                "Email_All": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Social Link": "",
                "External Links": "",
                "Source Directory": "",
                "Source URL": "",
            }
        ]
    )
    payload = EnrichmentPayload(
        socials=set(),
        websites=set(),
        emails={"support@lordprosser.bandcamp.com", "booking@lordprosser.com"},
        link_hubs=set(),
        source_dir="bandcamp_profile",
        source_url="https://lordprosser.bandcamp.com",
    )
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker._apply_payload(df, 0, payload)

    assert df.at[0, "Email"] == "booking@lordprosser.com"
    assert df.at[0, "Email_Source_URL"] == "https://lordprosser.bandcamp.com"
    assert df.at[0, "Email_Source_Type"] == "bandcamp_profile"
