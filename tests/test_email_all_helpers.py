import pandas as pd

from email_normalizer import filter_system_telemetry_emails
import pipeline_runner


def test_filter_system_telemetry_emails_removes_sentry_only():
    assert filter_system_telemetry_emails(["abc@o363271.ingest.us.sentry.io"]) == []


def test_filter_system_telemetry_emails_preserves_valid_contact():
    assert filter_system_telemetry_emails(
        ["abc@o363271.ingest.us.sentry.io", "contact@artist.com"]
    ) == ["contact@artist.com"]


def test_filter_system_telemetry_emails_leaves_valid_emails_unchanged():
    assert filter_system_telemetry_emails(
        ["booking@artist.com", "press@artist.com"]
    ) == ["booking@artist.com", "press@artist.com"]


def test_set_email_all_triggers_guard(monkeypatch):
    messages = []

    def fake_logger(msg):
        messages.append(msg)

    monkeypatch.setenv("EMAIL_ALL_GUARD", "1")
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist A",
                "Email": "a@example.com",
                "Email_All": "",
                "Directory_Email": "x@example.com",
            }
        ]
    )

    pipeline_runner._set_email_all(df, 0, "x@example.com", source="test_guard", logger=fake_logger)

    assert "x@example.com" in df.at[0, "Email_All"]
    assert not messages  # guard should not fire when email is row-local


def test_set_email_all_merges_and_logs(monkeypatch):
    messages = []

    def fake_logger(msg):
        messages.append(msg)

    monkeypatch.setenv("EMAIL_ALL_LOG", "1")
    df = pd.DataFrame([{"Artist Name": "Artist B", "Email_All": "a@example.com"}])

    merged = pipeline_runner._set_email_all(df, 0, ["a@example.com", "b@example.com"], source="test_merge", logger=fake_logger)

    assert merged == "a@example.com;b@example.com"
    assert df.at[0, "Email_All"] == "a@example.com;b@example.com"
    assert any("EmailAll" in msg for msg in messages)


def test_set_email_all_drops_telemetry_only_result():
    df = pd.DataFrame([{"Artist Name": "Artist C", "Email_All": ""}])

    merged = pipeline_runner._set_email_all(
        df,
        0,
        ["abc@o363271.ingest.us.sentry.io"],
        source="test_filter",
    )

    assert merged == ""
    assert df.at[0, "Email_All"] == ""
