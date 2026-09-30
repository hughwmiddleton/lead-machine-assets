import csv
import json

import pandas as pd

import pipeline_runner
from email_provenance import EMAIL_PROVENANCE_JSON_COL, _set_email_with_provenance
from lead_vault.exporter import FINAL_EXPORT_PRESET, WOODPECKER_EXPORT_PRESET, export_with_preset
from lead_vault.importer import ensure_master_csv_exists, import_csv_to_canonical_rows
from lead_vault.merge import merge_csv_into_master, preview_csv_import


PRODUCTION_DIAGNOSTIC_HEADERS = [
    "Bandcamp_Source_Mode",
    "Bandcamp_Search_Domain",
    "IG_Opportunity_State",
    "IG_Attempt_State",
    "IG_Extract_State",
    "IG_Write_State",
    "IG_Terminal_Reason",
    "IG_Execution_Path",
    "IG_Surface_Reason",
    "IG_Normalized_Terminal_Outcome",
    "IG_Normalized_Terminal_Reason",
    "FB_Opportunity_State",
    "FB_Gate_State",
    "FB_Attempt_State",
    "FB_Extract_State",
    "FB_Write_State",
    "FB_Debug_Reason",
    "FB_Terminal_Reason",
    "FB_Normalized_Terminal_Outcome",
    "FB_Normalized_Terminal_Reason",
    "BC_Status",
    "BC_Mode",
    "BC_Attempts",
    "BC_403_Count",
    "SC_Status",
    "SC_Reason",
    "SC_Fetches",
    "SC_ms",
    "Match_Score",
    "FB_Status",
    "__fb_discovery_attempted_this_run",
    "__fb_emails_applied",
    "origin_match_flag",
    "origin_match_reason",
    "origin_artist_score",
    "origin_title_score",
    "match_score_overall",
    "name_consistency_flag",
    "name_consistency_flag_polarity",
    "duplicate_email_flag",
    "duplicate_artist_flag",
    "directory_conflict_flag",
    "genre_outlier_flag",
    "FB_Refine_Decision",
    "FB_Refine_Executed",
    "FB_Name_Consistency_Flag",
]


def _provenance(email, source_type, surface, source_url, method="regex"):
    return json.dumps(
        {
            email: {
                "source_type": source_type,
                "surface": surface,
                "source_url": source_url,
                "extract_method": method,
            }
        },
        sort_keys=True,
    )


def _write_rows(path, headers, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def test_production_headers_map_or_ignore_while_unknown_requires_review(tmp_path):
    source_path = tmp_path / "production_shaped.csv"
    headers = [
        "Artist Name",
        "Country_Derived",
        "Spotify Playlist",
        "Email",
        "Email_All",
        "Email_Source_URL",
        "Email_Source_Type",
        "Email_Extract_Method",
        EMAIL_PROVENANCE_JSON_COL,
        "FB_Review_Reason",
        "Lead_Source",
        "Source_Directory",
        "Source URL",
        *PRODUCTION_DIAGNOSTIC_HEADERS,
        "Unfamiliar Semantic Field",
    ]
    _write_rows(source_path, headers, [{header: "" for header in headers}])

    preview = preview_csv_import(source_path)

    assert preview["mapped_headers"]["Country_Derived"] == "Country"
    assert preview["mapped_headers"]["Spotify Playlist"] == "Discovery_Source"
    assert preview["mapped_headers"][EMAIL_PROVENANCE_JSON_COL] == EMAIL_PROVENANCE_JSON_COL
    assert preview["mapped_headers"]["FB_Review_Reason"] == "Review_Reason"
    assert set(preview["ignored_headers"]) == set(PRODUCTION_DIAGNOSTIC_HEADERS)
    assert preview["unmapped_headers"] == ["Unfamiliar Semantic Field"]


def test_direct_import_reports_default_ignored_headers_and_preserves_provenance(tmp_path):
    source_path = tmp_path / "incoming.csv"
    email = "artist@example.test"
    provenance = _provenance(
        email,
        "instagram_enrich",
        "instagram_profile",
        "https://www.instagram.com/example/",
    )
    headers = ["Artist Name", "Email", EMAIL_PROVENANCE_JSON_COL, "FB_Debug_Reason"]
    _write_rows(
        source_path,
        headers,
        [{"Artist Name": "Example", "Email": email, EMAIL_PROVENANCE_JSON_COL: provenance, "FB_Debug_Reason": "trace"}],
    )

    result = import_csv_to_canonical_rows(source_path)

    assert result["ignored_headers"] == ["FB_Debug_Reason"]
    assert result["unmapped_headers"] == []
    assert result["canonical_rows"][0][EMAIL_PROVENANCE_JSON_COL] == provenance


def test_instagram_surface_survives_facebook_handoff_and_facebook_stays_facebook():
    direct_email = "direct@example.test"
    direct_url = "https://www.instagram.com/direct/"
    direct = pd.Series(
        {
            "Email": direct_email,
            "Email_Source_Type": "instagram_enrich",
            "Email_Source_URL": direct_url,
            EMAIL_PROVENANCE_JSON_COL: _provenance(
                direct_email, "instagram_enrich", "instagram_profile", direct_url
            ),
        }
    )
    assert pipeline_runner._facebook_email_surface_hint(direct) == "instagram_profile"

    direct_df = pd.DataFrame([direct])
    _set_email_with_provenance(
        (direct_df, 0),
        direct_email,
        direct_url,
        "instagram_enrich",
        "regex",
        pipeline_runner._facebook_email_surface_hint(direct),
    )
    assert json.loads(direct_df.at[0, EMAIL_PROVENANCE_JSON_COL])[direct_email]["surface"] == "instagram_profile"

    one_hop_email = "onehop@example.test"
    one_hop_url = "https://artist.example/contact"
    one_hop = pd.Series(
        {
            "Email": one_hop_email,
            "Email_Source_Type": "instagram_enrich",
            "Email_Source_URL": one_hop_url,
            EMAIL_PROVENANCE_JSON_COL: _provenance(
                one_hop_email,
                "instagram_enrich",
                "instagram_bio_link_one_hop",
                one_hop_url,
            ),
        }
    )
    assert pipeline_runner._facebook_email_surface_hint(one_hop) == "instagram_bio_link_one_hop"

    soundcloud_email = "soundcloud@example.test"
    soundcloud_url = "https://soundcloud.com/example"
    soundcloud = pd.Series(
        {
            "Email": soundcloud_email,
            "Email_Source_Type": "soundcloud",
            "Email_Source_URL": soundcloud_url,
            EMAIL_PROVENANCE_JSON_COL: _provenance(
                soundcloud_email,
                "soundcloud",
                "soundcloud_profile",
                soundcloud_url,
            ),
        }
    )
    assert pipeline_runner._facebook_email_surface_hint(soundcloud) == "soundcloud_profile"

    facebook_main = pd.Series(
        {
            "Email": "fb@example.test",
            "Email_Source_Type": "facebook_enrich",
            "Email_Source_URL": "https://www.facebook.com/example",
        }
    )
    facebook_about = facebook_main.copy()
    facebook_about["Email_Source_URL"] = "https://www.facebook.com/example/about"
    assert pipeline_runner._facebook_email_surface_hint(facebook_main) == "facebook_main"
    assert pipeline_runner._facebook_email_surface_hint(facebook_about) == "facebook_about"


def test_lead_vault_round_trip_preserves_contact_provenance_and_origin(tmp_path):
    source_path = tmp_path / "enriched.csv"
    master_path = tmp_path / "master.csv"
    full_export_path = tmp_path / "review.csv"
    woodpecker_path = tmp_path / "woodpecker.csv"
    ensure_master_csv_exists(master_path)

    headers = [
        "Artist Name",
        "Email",
        "Email_All",
        "Email Source",
        "Email_Source_URL",
        "Email_Source_Type",
        "Email_Extract_Method",
        EMAIL_PROVENANCE_JSON_COL,
        "Facebook_URL",
        "Instagram_URL",
        "Website",
        "Bandcamp_URL",
        "Lead_Source",
        "Source_Directory",
        "Source Directory",
        "Source URL",
        "final_status",
        "Needs_Review",
    ]
    cases = [
        ("IG Artist", "ig@artist.test", "instagram_enrich", "instagram_profile", "https://www.instagram.com/igartist/"),
        ("FB Artist", "fb@artist.test", "facebook_enrich", "facebook_main", "https://www.facebook.com/fbartist"),
        ("Web Artist", "web@artist.test", "website_enrich", "website_contact_page", "https://webartist.test/contact"),
        ("Bandcamp Artist", "bandcamp@artist.test", "bandcamp", "bandcamp_profile", "https://artist.bandcamp.com"),
    ]
    rows = []
    for artist, email, source_type, surface, source_url in cases:
        rows.append(
            {
                "Artist Name": artist,
                "Email": email,
                "Email_All": email,
                "Email Source": source_type,
                "Email_Source_URL": source_url,
                "Email_Source_Type": source_type,
                "Email_Extract_Method": "regex",
                EMAIL_PROVENANCE_JSON_COL: _provenance(email, source_type, surface, source_url),
                "Facebook_URL": source_url if source_type == "facebook_enrich" else "",
                "Instagram_URL": source_url if source_type == "instagram_enrich" else "",
                "Website": source_url if source_type == "website_enrich" else "",
                "Bandcamp_URL": source_url if source_type == "bandcamp" else "",
                "Lead_Source": "Unearthed" if artist == "IG Artist" else "Spotify",
                "Source_Directory": "Unearthed" if artist == "IG Artist" else "Spotify",
                "Source Directory": "Unearthed" if artist == "IG Artist" else "Spotify",
                "Source URL": f"https://www.abc.net.au/triplejunearthed/artist/ig-artist" if artist == "IG Artist" else f"https://source.test/{artist.lower().replace(' ', '-')}",
                "final_status": "OK",
                "Needs_Review": "FALSE",
            }
        )
    rows.append(
        {
            "Artist Name": "No Email Artist",
            "Lead_Source": "Unearthed",
            "Source_Directory": "Unearthed",
            "Source Directory": "Unearthed",
            "Source URL": "https://www.abc.net.au/triplejunearthed/artist/no-email-artist",
            "final_status": "WARN",
            "Needs_Review": "TRUE",
        }
    )
    _write_rows(source_path, headers, rows)

    imported = merge_csv_into_master(source_path, master_path=master_path)
    assert imported["unmapped_headers"] == []
    assert imported["rows_added"] == 5

    duplicate_path = tmp_path / "duplicate.csv"
    alternate = "alt@artist.test"
    alternate_url = "https://igartist.test/contact"
    duplicate_provenance = json.dumps(
        {
            "ig@artist.test": {
                "source_type": "live_search",
                "surface": "live_search",
                "source_url": "https://weak-search.test/result",
                "extract_method": "regex",
            },
            alternate: {
                "source_type": "website_enrich",
                "surface": "website_contact_page",
                "source_url": alternate_url,
                "extract_method": "mailto",
            },
        },
        sort_keys=True,
    )
    _write_rows(
        duplicate_path,
        headers,
        [
            {
                "Artist Name": "IG Artist",
                "Email": alternate,
                "Email_All": alternate,
                "Email_Source_URL": alternate_url,
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "mailto",
                EMAIL_PROVENANCE_JSON_COL: duplicate_provenance,
                "Lead_Source": "Spotify",
                "Source_Directory": "Spotify",
                "Source Directory": "Spotify",
                "Source URL": "https://www.abc.net.au/triplejunearthed/artist/ig-artist",
                "final_status": "OK",
                "Needs_Review": "FALSE",
            }
        ],
    )

    consolidated = merge_csv_into_master(
        duplicate_path,
        master_path=master_path,
        duplicate_strategy="merge_consolidate",
    )
    assert consolidated["rows_duplicates_detected"] == 1

    with master_path.open(encoding="utf-8-sig", newline="") as handle:
        stored = list(csv.DictReader(handle))
    for artist, email, source_type, surface, source_url in cases:
        stored_row = next(row for row in stored if row["Artist"] == artist)
        assert stored_row["Primary_Email"] == email
        if artist == "IG Artist":
            assert email in stored_row["All_Emails"].split(";")
        else:
            assert stored_row["All_Emails"] == email
        assert stored_row["Email_Source_Type"] == source_type
        assert stored_row["Email_Source_URL"] == source_url
        assert stored_row["Email_Extract_Method"] == "regex"
        stored_meta = json.loads(stored_row[EMAIL_PROVENANCE_JSON_COL])[email]
        assert stored_meta["surface"] == surface
        assert stored_meta["extract_method"] == "regex"

    ig_row = next(row for row in stored if row["Artist"] == "IG Artist")
    assert ig_row["Primary_Email"] == "ig@artist.test"
    assert set(ig_row["All_Emails"].split(";")) == {"ig@artist.test", alternate}
    merged_provenance = json.loads(ig_row[EMAIL_PROVENANCE_JSON_COL])
    assert set(merged_provenance) == {"ig@artist.test", alternate}
    assert merged_provenance["ig@artist.test"]["surface"] == "instagram_profile"
    assert merged_provenance[alternate]["surface"] == "website_contact_page"
    assert ig_row["Email_Source_Type"] == "instagram_enrich"
    assert ig_row["Email_Source_URL"] == "https://www.instagram.com/igartist/"
    assert ig_row["Lead_Source"] == "Unearthed"
    assert ig_row["Source_Directory"] == "Unearthed"
    assert ig_row["Source_URL"] == "https://www.abc.net.au/triplejunearthed/artist/ig-artist"

    no_email_row = next(row for row in stored if row["Artist"] == "No Email Artist")
    assert no_email_row["Primary_Email"] == ""
    assert no_email_row[EMAIL_PROVENANCE_JSON_COL] == ""

    export_with_preset(FINAL_EXPORT_PRESET, master_path, full_export_path)
    with full_export_path.open(encoding="utf-8-sig", newline="") as handle:
        reviewed = list(csv.DictReader(handle))
    reviewed_ig = next(row for row in reviewed if row["Artist Name"] == "IG Artist")
    assert reviewed_ig[EMAIL_PROVENANCE_JSON_COL] == ig_row[EMAIL_PROVENANCE_JSON_COL]

    export_with_preset(WOODPECKER_EXPORT_PRESET, master_path, woodpecker_path)
    with woodpecker_path.open(encoding="utf-8-sig", newline="") as handle:
        woodpecker_headers = next(csv.reader(handle))
    assert EMAIL_PROVENANCE_JSON_COL not in woodpecker_headers
