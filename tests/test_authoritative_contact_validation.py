import json

import pandas as pd
import pytest

import final_checker


def _row(artist: str, links: str, emails: list[tuple[str, ...]]) -> dict:
    provenance = {
        email: {
            "source_type": source_type,
            "source_url": source_url,
            "extract_method": "regex",
            "role": role,
            "role_evidence": "test",
        }
        for email, source_url, source_type, role in (
            (*item, "unknown") if len(item) == 3 else item for item in emails
        )
    }
    email_values = list(provenance)
    return {
        "Artist Name": artist,
        "Social Link": links,
        "Email": email_values[0] if email_values else "",
        "Email_All": ";".join(email_values),
        "Email_Provenance_JSON": json.dumps(provenance),
        "final_status": "OK",
    }


@pytest.mark.parametrize(
    ("artist", "links", "expected"),
    [
        (
            "Yung Milla",
            "https://www.facebook.com/YUNG-MILLA-392355807989428/ | https://instagram.com/yungmilla_",
            "matched",
        ),
        (
            "Effie Isobel",
            "https://facebook.com/musicbyeffie | https://instagram.com/effie.isobel",
            "matched",
        ),
        (
            "Yung Maynie",
            "https://facebook.com/OfficialYungMaynie | https://instagram.com/yungmaynie_",
            "matched",
        ),
    ],
)
def test_benign_platform_handle_variants_match_canonical_artist(artist, links, expected):
    assessment = final_checker.assess_artist_identity(_row(artist, links, []))

    assert assessment["status"] == expected
    assert assessment["directory_conflict"] == 0


def test_one_unrelated_soundcloud_candidate_does_not_poison_corroborated_alias():
    row = _row(
        "Younique",
        "https://soundcloud.com/corey-ernie | https://facebook.com/youniqueflavour | https://instagram.com/youniqueflavour",
        [],
    )

    assessment = final_checker.assess_artist_identity(row)

    assert assessment["status"] == "plausible_alias"
    assert assessment["directory_conflict"] == 0
    assert {item["state"] for item in assessment["candidates"]} == {"plausible_alias", "rejected"}


def test_single_unmatched_surface_at_exact_legacy_boundary_requires_review():
    email = "skinny@sidequest.com.au"
    row = _row(
        "Clancy",
        "https://instagram.com/chefspiss_",
        [(email, "https://instagram.com/chefspiss_", "instagram_enrich", "unknown")],
    )
    row["match_score_overall"] = "0.75"

    validated = final_checker.apply_authoritative_validation(row, [row])
    flags = {
        "name_flag": 1,
        "dir_conflict_flag": 1,
        "dup_email_flag": 0,
        "dup_artist_flag": 0,
    }

    assert validated["match_score_overall"] < 0.75
    assert validated["Preferred_Contact_Status"] == "WARN"
    assert final_checker.compute_final_status(validated, flags, validated["match_score_overall"]) == "WARN"


def test_contacts_are_independent_and_artist_direct_is_preferred():
    direct = "hello@stimpies.band"
    alternate = "pjbyrne999@gmail.com"
    row = _row(
        "Stimpies",
        "https://facebook.com/stimpiess | https://instagram.com/stimpies",
        [
            (direct, "https://facebook.com/stimpiess", "facebook_enrich", "artist_direct"),
            (alternate, "https://instagram.com/stimpies", "instagram_enrich", "unknown"),
        ],
    )

    validated = final_checker.apply_authoritative_validation(row, [row])
    provenance = json.loads(validated["Email_Provenance_JSON"])

    assert validated["Preferred_Outreach_Email"] == direct
    assert validated["Alternate_Emails"] == alternate
    assert provenance[direct]["preferred"] == "true"
    assert provenance[alternate]["preferred"] == "false"
    assert provenance[direct]["validation_status"] == "OK"
    assert provenance[alternate]["validation_status"] in {"OK", "WARN"}


def test_safe_contact_does_not_rescue_contaminated_alternate():
    safe = "booking@meiia.com"
    contaminated = "hello@stimpies.band"
    owner = _row(
        "Stimpies",
        "https://facebook.com/stimpiess",
        [(contaminated, "https://facebook.com/stimpiess", "facebook_enrich", "artist_direct")],
    )
    row = _row(
        "Meiia",
        "https://instagram.com/meiialiveshere | https://facebook.com/stimpiess",
        [
            (safe, "https://instagram.com/meiialiveshere", "instagram_enrich", "booking"),
            (contaminated, "https://facebook.com/stimpiess", "facebook_enrich", "unknown"),
        ],
    )

    validated = final_checker.apply_authoritative_validation(row, [row, owner])
    provenance = json.loads(validated["Email_Provenance_JSON"])

    assert validated["Preferred_Outreach_Email"] == safe
    assert provenance[safe]["validation_status"] == "OK"
    assert provenance[contaminated]["validation_status"] == "BLOCK"
    assert provenance[contaminated]["send_eligible"] == "false"


def test_shared_management_contact_is_not_blocked_as_duplicate():
    email = "management@example.com"
    first = _row(
        "Artist One",
        "https://instagram.com/artistone",
        [(email, "https://instagram.com/artistone", "instagram_enrich", "management")],
    )
    second = _row(
        "Artist Two",
        "https://instagram.com/artisttwo",
        [(email, "https://instagram.com/artisttwo", "instagram_enrich", "management")],
    )

    assert not final_checker.duplicate_email_is_unsafe(first, [first, second], email=email)
    assert not final_checker.duplicate_email_is_unsafe(second, [first, second], email=email)


def test_private_facebook_route_contact_is_blocked():
    email = "artist@example.com"
    row = _row(
        "Artist",
        "https://instagram.com/artist",
        [(email, "https://m.me/artist", "facebook_enrich", "artist_direct")],
    )

    validated = final_checker.apply_authoritative_validation(row, [row])
    provenance = json.loads(validated["Email_Provenance_JSON"])

    assert provenance[email]["validation_status"] == "BLOCK"
    assert validated["Preferred_Contact_Status"] == "BLOCK"


def test_preferred_selection_is_stable_for_multiple_professional_contacts():
    row = _row(
        "Public Figures",
        "https://instagram.com/publicfigures4eva",
        [
            ("cara@slowclap.com.au", "https://instagram.com/publicfigures4eva", "instagram_enrich", "management"),
            ("jake@damagedmusic.com.au", "https://instagram.com/publicfigures4eva", "instagram_enrich", "booking"),
            ("rebecca@collectiveartists.com.au", "https://instagram.com/publicfigures4eva", "instagram_enrich", "pr_publicity"),
        ],
    )

    first = final_checker.apply_authoritative_validation(row, [row])
    second = final_checker.apply_authoritative_validation(row, [row])

    assert first["Preferred_Outreach_Email"] == "cara@slowclap.com.au"
    assert first["Preferred_Outreach_Email"] == second["Preferred_Outreach_Email"]
    assert len(first["Alternate_Emails"].split(";")) == 2


def test_run_final_checker_refreshes_stale_status_reasons(tmp_path):
    owner = _row(
        "Stimpies",
        "https://facebook.com/stimpiess",
        [("hello@stimpies.band", "https://facebook.com/stimpiess", "facebook_enrich", "artist_direct")],
    )
    rows = [
        _row(
            "Yung Milla",
            "https://www.facebook.com/YUNG-MILLA-392355807989428/ | https://instagram.com/yungmilla_",
            [("aum@aum.net.au", "https://instagram.com/yungmilla_", "instagram_enrich", "unknown")],
        ),
        _row(
            "Effie Isobel",
            "https://facebook.com/musicbyeffie | https://instagram.com/effie.isobel",
            [("hello@effiemusic.com", "https://facebook.com/musicbyeffie", "facebook_enrich", "artist_direct")],
        ),
        _row(
            "Younique",
            "https://facebook.com/youniqueflavour | https://instagram.com/youniqueflavour",
            [("management@youniquemusic.com", "https://facebook.com/youniqueflavour", "facebook_enrich", "management")],
        ),
        _row(
            "Clancy",
            "https://instagram.com/chefspiss_",
            [("skinny@sidequest.com.au", "https://instagram.com/chefspiss_", "instagram_enrich", "unknown")],
        ),
        _row(
            "Meiia",
            "https://instagram.com/meiialiveshere | https://facebook.com/stimpiess",
            [
                ("booking@meiia.com", "https://instagram.com/meiialiveshere", "instagram_enrich", "booking"),
                ("hello@stimpies.band", "https://facebook.com/stimpiess", "facebook_enrich", "unknown"),
            ],
        ),
        owner,
    ]
    for row in rows:
        row["Final_Status_Reason"] = "identity_unresolved"
        row["final_status"] = "OK" if row["Artist Name"] == "Clancy" else "BLOCK"

    input_path = tmp_path / "golden.csv"
    pd.DataFrame(rows).to_csv(input_path, index=False)
    checked_path = final_checker.run_final_checker(str(input_path))
    checked = pd.read_csv(checked_path, dtype=str, keep_default_na=False)
    by_artist = checked.set_index("Artist Name")

    assert by_artist.loc["Yung Milla", ["final_status", "Final_Status_Reason"]].tolist() == [
        "OK", "preferred_contact_accepted"
    ]
    assert by_artist.loc["Effie Isobel", ["final_status", "Final_Status_Reason"]].tolist() == [
        "OK", "preferred_contact_accepted"
    ]
    assert by_artist.loc["Younique", ["final_status", "Final_Status_Reason"]].tolist() == [
        "OK", "preferred_contact_accepted"
    ]
    assert by_artist.loc["Clancy", ["final_status", "Final_Status_Reason"]].tolist() == [
        "WARN", "source_identity_unresolved"
    ]
    assert by_artist.loc["Meiia", ["final_status", "Final_Status_Reason"]].tolist() == [
        "BLOCK", "duplicate_email"
    ]
