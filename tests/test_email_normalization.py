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


def test_night_fb_text_sample_direct_email_uses_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_normalize(_sample: str):  # noqa: ANN001
        raise AssertionError("direct email path should not normalize")

    monkeypatch.setattr(night_mode_fb, "_normalize_fb_obfuscated_email_text", fail_normalize)

    emails = night_mode_fb._extract_fb_emails_from_text_sample("Bookings: bookings@artist.com")

    assert emails == ["bookings@artist.com"]


def test_night_fb_text_sample_obfuscated_email_still_normalizes() -> None:
    emails = night_mode_fb._extract_fb_emails_from_text_sample("Bookings: bookings (at) artist (dot) com")

    assert emails == ["bookings@artist.com"]


def test_night_fb_text_sample_plain_non_email_skips_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_normalize(_sample: str):  # noqa: ANN001
        raise AssertionError("plain non-email path should not normalize")

    monkeypatch.setattr(night_mode_fb, "_normalize_fb_obfuscated_email_text", fail_normalize)

    emails = night_mode_fb._extract_fb_emails_from_text_sample(
        "This is a long plain text blob with profile copy, tour dates, and no contact address."
    )

    assert emails == []


@pytest.mark.parametrize(
    ("sample", "junk_candidate"),
    [
        ("Members only @ savagex.com. Select your region", "only@savagex.com.select"),
        ("Book online @ www.aaa.com today", "online@www.aaa.com"),
        ("While away @ school. Associate programs are open", "away@school.associate"),
        ("Fresh meals @ home. With delivery available", "meals@home.with"),
        ("Subscribers only @ www.gobble.com now", "only@www.gobble.com"),
    ],
)
def test_night_fb_raw_admission_rejects_obvious_junk_email_like_fragments(sample: str, junk_candidate: str) -> None:
    emails = night_mode_fb._extract_fb_emails_from_text_sample(sample)

    assert junk_candidate not in emails
    assert emails == []


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ("Bookings: bookings@artist.com", "bookings@artist.com"),
        ("Bookings: name @ artist . com", "name@artist.com"),
        ("Press: info@artist.co.uk", "info@artist.co.uk"),
        ("Direct: hello@sub.artist.com", "hello@sub.artist.com"),
    ],
)
def test_night_fb_raw_admission_still_keeps_valid_contact_emails(sample: str, expected: str) -> None:
    assert night_mode_fb._extract_fb_emails_from_text_sample(sample) == [expected]


def test_night_fb_extract_html_keeps_valid_contacts_while_dropping_junk_fragments() -> None:
    html = """
    <html>
      <body>
        Members only @ savagex.com. Select your region.
        Book online @ www.aaa.com today.
        Contact bookings@artist.com for management.
        Press: press @ label . co . uk
      </body>
    </html>
    """

    emails, used_mailto = night_mode_fb._extract_emails_from_html(html)

    assert emails == ["bookings@artist.com", "press@label.co.uk"]
    assert used_mailto is False


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ("Bookings: bookings@artist.com", False),
        ("Bookings: bookings (at) artist (dot) com", True),
        ("Bookings: bookings [at] artist [dot] com", True),
        ("Bookings: bookings at artist dot com", True),
        ("Bookings: bookings @ artist . com", True),
        ("https://example.com/contact?email=press%40artist.com", True),
        ("Just a plain biography paragraph without contact info", False),
    ],
)
def test_night_fb_obfuscation_guard_distinguishes_needed_normalization(sample: str, expected: bool) -> None:
    assert night_mode_fb._fb_sample_needs_obfuscation_normalization(sample) is expected
