import pandas as pd

import pipeline_runner


def test_set_email_all_triggers_guard(monkeypatch):
    messages = []

    def fake_logger(msg):
        messages.append(msg)

    monkeypatch.setenv("EMAIL_ALL_GUARD", "1")
    df = pd.DataFrame([{"Artist Name": "Artist A", "Email": "a@example.com", "Email_All": ""}])

    pipeline_runner._set_email_all(df, 0, "x@example.com", source="test_guard", logger=fake_logger)

    assert "email_not_in_sources" in " ".join(messages)
    assert "x@example.com" in df.at[0, "Email_All"]


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
