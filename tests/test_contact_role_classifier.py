import json

import pandas as pd
import pytest

from contact_role_classifier import classify_email_role
from email_provenance import (
    EMAIL_PROVENANCE_JSON_COL,
    EMAIL_ROLE_COL,
    EMAIL_ROLE_EVIDENCE_COL,
    apply_email_role_metadata,
)
from pipeline_runner import _build_final_export_frame, _consolidate_email_all


@pytest.mark.parametrize(
    ("email", "artist", "source_url", "expected"),
    [
        ("cheapskatetheband@gmail.com", "CHEAP-SKATE", "", "artist_direct"),
        ("nemiphnx@gmail.com", "NEMI PHNX", "", "artist_direct"),
        ("management@filldaparty.com", "Fill Da Party", "", "management"),
        ("info@opendoormgmt.com.au", "Some Artist", "", "management"),
        ("bookings@exampleagency.com", "Some Artist", "", "booking"),
        ("press@examplepublicity.com", "Some Artist", "", "pr_publicity"),
        ("pr@examplepublicity.com", "Some Artist", "", "pr_publicity"),
        ("label@example-records.com", "Some Artist", "", "label"),
        ("hello@exampleagency.com", "Some Artist", "https://exampleagency.com/booking", "booking"),
        ("team@example.com", "Some Artist", "", "team_business"),
    ],
)
def test_deterministic_role_rules(email: str, artist: str, source_url: str, expected: str) -> None:
    first = classify_email_role(email, artist_name=artist, source_url=source_url)
    second = classify_email_role(email, artist_name=artist, source_url=source_url)

    assert first == second
    assert first.role == expected
    assert first.evidence


@pytest.mark.parametrize(
    ("email", "artist"),
    [
        ("hello@gmail.com", "Some Artist"),
        ("info@exampleagency.com", "Some Artist"),
        ("person@gmail.com", "Some Artist"),
        ("hello@someartistmusic.com", "Different Artist"),
    ],
)
def test_weak_or_generic_signals_remain_unknown(email: str, artist: str) -> None:
    result = classify_email_role(email, artist_name=artist)

    assert result.role == "unknown"
    assert result.evidence == "insufficient_evidence"


def test_context_role_is_used_without_overloading_source_type() -> None:
    result = classify_email_role(
        "info@exampleagency.com",
        artist_name="Some Artist",
        row_context={"Domain_Role": "Management company"},
    )

    assert result.role == "management"
    assert result.evidence == "context:Domain_Role:management"


def test_multiple_emails_keep_independent_roles_in_provenance() -> None:
    row = {
        "Artist Name": "NEMI PHNX",
        "Email": "management@filldaparty.com",
        "Email_All": "management@filldaparty.com;nemiphnx@gmail.com",
        "Email_Type": "website_enrich",
        "Email_Source_Type": "website_enrich",
        "Email_Source_URL": "https://filldaparty.com/management",
        "Email_Extract_Method": "mailto",
    }

    apply_email_role_metadata(row)

    provenance = json.loads(row[EMAIL_PROVENANCE_JSON_COL])
    assert row[EMAIL_ROLE_COL] == "management"
    assert row[EMAIL_ROLE_EVIDENCE_COL] == "local_part:management"
    assert provenance["management@filldaparty.com"]["role"] == "management"
    assert provenance["nemiphnx@gmail.com"]["role"] == "artist_direct"
    assert row["Email_Type"] == "website_enrich"
    assert row["Email_Source_Type"] == "website_enrich"


def test_primary_contact_role_is_not_blindly_copied_to_other_emails() -> None:
    row = {
        "Artist Name": "Some Artist",
        "Email": "info@example.com",
        "Email_All": "info@example.com;other@gmail.com",
        "Contact_Role": "Manager",
        "Email_Source_URL": "https://example.com/management",
    }

    apply_email_role_metadata(row)
    provenance = json.loads(row[EMAIL_PROVENANCE_JSON_COL])

    assert provenance["info@example.com"]["role"] == "management"
    assert provenance["other@gmail.com"]["role"] == "unknown"


def test_consolidation_backfills_roles_for_legacy_rows() -> None:
    df = pd.DataFrame(
        [
            {
                "Artist Name": "NEMI PHNX",
                "Email": "nemiphnx@gmail.com",
                "Email_All": "nemiphnx@gmail.com;management@filldaparty.com",
            }
        ]
    )

    result = _consolidate_email_all(df)
    provenance = json.loads(result.iloc[0][EMAIL_PROVENANCE_JSON_COL])

    assert result.iloc[0][EMAIL_ROLE_COL] == "artist_direct"
    assert provenance["management@filldaparty.com"]["role"] == "management"


@pytest.mark.parametrize("email", ["management@filldaparty.com", "hello@gmail.com"])
def test_representative_or_unknown_contact_remains_exportable(email: str) -> None:
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Fill Da Party",
                "Email": email,
                "Email_All": email,
                "Email_Type": "website_enrich",
                "Email_Source_URL": "https://filldaparty.com/contact",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "mailto",
                "final_status": "OK",
                "Needs_Review": "FALSE",
            }
        ]
    )

    export = _build_final_export_frame(df)

    assert len(export) == 1
    assert export.iloc[0]["Email"] == email
    assert export.iloc[0]["Email_All"] == email
    assert export.iloc[0][EMAIL_ROLE_COL] in {"management", "unknown"}
    assert email in json.loads(export.iloc[0][EMAIL_PROVENANCE_JSON_COL])
