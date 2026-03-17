import pytest

import night_mode_fb
import website_email_scraper
import pipeline_runner


def test_obfuscated_email_is_normalized_and_extracted():
    logs = []
    html = "<html><body>Contact: bookings(at)Artist.com or info [at] artist.com</body></html>"

    emails = website_email_scraper._extract_emails_from_html(html, logger=logs.append)

    assert "bookings@artist.com" in emails
    assert "info@artist.com" in emails
    assert any("normalized pattern" in msg for msg in logs)


def test_email_normalization_and_dedupe():
    merged = pipeline_runner._merge_email_lists(
        ["booking@artist.com", " Booking@Artist.com "],
        "booking@artist.com",
    )
    assert merged == ["booking@artist.com"]


def test_invalid_obfuscation_does_not_produce_email():
    html = "<html><body>Reach us at contact at example</body></html>"
    emails = website_email_scraper._extract_emails_from_html(html)
    assert emails == []


@pytest.mark.parametrize(
    ("rendered_text", "expected"),
    [
        ("Bookings: name [at] artist [dot] com", "name@artist.com"),
        ("Bookings: name(at)artist(dot)co(dot)uk", "name@artist.co.uk"),
        ("Bookings: name @ artist . com", "name@artist.com"),
        ("Bookings: name ＠ artist ． com", "name@artist.com"),
    ],
)
def test_night_fb_extracts_obfuscated_rendered_email_variants(rendered_text: str, expected: str) -> None:
    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        "<html><body><div>No visible email</div></body></html>",
        rendered_text=rendered_text,
    )

    assert emails == [expected]
    assert used_mailto is False


def test_night_fb_extracts_obfuscated_anchor_email_value() -> None:
    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        "<html><body><div>No visible email</div></body></html>",
        anchor_values=["https://example.com/contact?email=press%20at%20artist%20dot%20com"],
    )

    assert emails == ["press@artist.com"]
    assert used_mailto is False


def test_night_fb_invalid_obfuscation_does_not_produce_email() -> None:
    emails, used_mailto = night_mode_fb._extract_emails_from_html(
        "<html><body><div>No visible email</div></body></html>",
        rendered_text="Reach us at contact at example",
    )

    assert emails == []
    assert used_mailto is False
