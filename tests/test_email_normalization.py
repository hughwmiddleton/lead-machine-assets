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
