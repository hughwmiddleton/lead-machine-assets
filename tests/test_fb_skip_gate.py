import pandas as pd

from pipeline_runner import _cell_str  # reuse for pd.NA safety
from pipeline_runner import run_facebook_global_pass  # pragma: no cover - ensure import works

# Import the helper directly for focused testing.
from pipeline_runner import _should_skip_row_due_to_email  # type: ignore


def _row(**kwargs) -> pd.Series:
    return pd.Series(kwargs)


def test_skip_gate_allows_shared_placeholder_email():
    row = _row(Email_All="user@example.com", **{"Artist Name": "Artist A"})
    assert _should_skip_row_due_to_email(row) is False


def test_skip_gate_allows_placeholder_only_email():
    row = _row(Email_All="user@domain.com", **{"Artist Name": "Artist Placeholder"})
    assert _should_skip_row_due_to_email(row) is False


def test_skip_gate_skips_when_real_email_present():
    row = _row(Email_All="artist@gmail.com", **{"Artist Name": "Artist Real"})
    assert _should_skip_row_due_to_email(row) is True


def test_skip_gate_skips_when_placeholder_and_real_email_present():
    row = _row(
        Email_All="user@domain.com;artist@gmail.com",
        **{"Artist Name": "Artist Mixed"},
    )
    assert _should_skip_row_due_to_email(row) is True


def test_skip_gate_allows_when_quarantined_even_with_email():
    messages = []
    row = _row(
        Email_All="user@example.com",
        **{"Email Source": "Quarantined (repeat email)"},
        __row_id=5,
        **{"Artist Name": "Artist B"},
    )
    assert _should_skip_row_due_to_email(row, True, messages.append) is False
    assert messages  # override should log
    assert "row 5 ('Artist B')" in messages[0]


def test_skip_gate_allows_when_suspect_present():
    row = _row(
        Email_All="user@example.com",
        Suspect_Email="suspect@example.com",
        **{"Artist Name": "Artist C"},
    )
    assert _should_skip_row_due_to_email(row) is False
