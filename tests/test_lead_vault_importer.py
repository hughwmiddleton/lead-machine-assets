import lead_vault.alias_map
import lead_vault.importer
import lead_vault.schema
from lead_vault.alias_map import map_headers_to_canonical, normalize_header
from lead_vault.importer import build_canonical_row, import_csv_to_canonical_rows
from lead_vault.schema import get_canonical_master_schema


def test_package_imports_still_work() -> None:
    assert hasattr(lead_vault.alias_map, "map_headers_to_canonical")
    assert hasattr(lead_vault.importer, "import_csv_to_canonical_rows")
    assert hasattr(lead_vault.schema, "get_canonical_master_schema")


def test_normalize_header_handles_case_spacing_and_bom() -> None:
    assert normalize_header("\ufeff Final_Status ") == "final status"
    assert normalize_header("Facebook URL") == "facebook url"
    assert normalize_header("facebook_url") == "facebook url"
    assert normalize_header("facebook-url") == "facebook url"
    assert normalize_header("  Played   on__Unearthed ") == "played on unearthed"


def test_alias_examples_map_correctly() -> None:
    mapped = map_headers_to_canonical(
        [
            "Artist Name",
            "Band Name",
            "Email",
            "E-mail",
            "All Emails",
            "Sounds Like",
            "Social Link",
            "Genre",
            "Unearthed_Genre_Raw",
            "Source URL",
            "Email Type",
            "facebook_url",
            "Facebook URL",
            "SoundCloud Link",
            "Source Directory",
            "Date Added",
            "final_status",
            "Played on triple J",
            "Played on Unearthed",
        ]
    )

    assert mapped["Artist Name"] == "Artist"
    assert mapped["Band Name"] == "Artist"
    assert mapped["Email"] == "Primary_Email"
    assert mapped["E-mail"] == "Primary_Email"
    assert mapped["All Emails"] == "All_Emails"
    assert mapped["Sounds Like"] == "Sounds Like"
    assert mapped["Social Link"] == "Social Link"
    assert mapped["Genre"] == "Primary_Genre"
    assert mapped["Unearthed_Genre_Raw"] == "Unearthed_Genre_Raw"
    assert mapped["Source URL"] == "Source_URL"
    assert mapped["Email Type"] == "Email_Type"
    assert mapped["facebook_url"] == "Facebook_URL"
    assert mapped["Facebook URL"] == "Facebook_URL"
    assert mapped["SoundCloud Link"] == "SoundCloud_URL"
    assert mapped["Source Directory"] == "Source_Directory"
    assert mapped["Date Added"] == "Date_Added"
    assert mapped["final_status"] == "Final_Status"
    assert mapped["Played on triple J"] == "Played_On_Triple_J"
    assert mapped["Played on Unearthed"] == "Played_On_Unearthed"


def test_import_csv_produces_canonical_rows_only_and_surfaces_unmapped_headers(tmp_path) -> None:
    input_csv = tmp_path / "input.csv"
    input_csv.write_text(
        "Artist Name,Email,All Emails,Source Directory,Unknown Column\n"
        "The Act,artist@example.com,artist@example.com;mgr@example.com,spotify,surprise\n",
        encoding="utf-8",
    )

    result = import_csv_to_canonical_rows(input_csv)

    assert result["detected_headers"] == [
        "Artist Name",
        "Email",
        "All Emails",
        "Source Directory",
        "Unknown Column",
    ]
    assert result["encoding"] == "utf-8-sig"
    assert result["mapped_headers"] == {
        "Artist Name": "Artist",
        "Email": "Primary_Email",
        "All Emails": "All_Emails",
        "Source Directory": "Source_Directory",
    }
    assert result["unmapped_headers"] == ["Unknown Column"]
    assert result["row_count"] == 1

    row = result["canonical_rows"][0]
    assert list(row.keys()) == get_canonical_master_schema()
    assert row["Artist"] == "The Act"
    assert row["Primary_Email"] == "artist@example.com"
    assert row["All_Emails"] == "artist@example.com;mgr@example.com"
    assert row["Source_Directory"] == "spotify"
    assert "Unknown Column" not in row


def test_extra_columns_do_not_break_import_and_missing_values_become_empty_strings(tmp_path) -> None:
    input_csv = tmp_path / "missing_values.csv"
    input_csv.write_text(
        "Artist Name,Email,Source URL,Spare Column\n"
        "Act One,,https://example.com,extra\n"
        "Act Two\n",
        encoding="utf-8",
    )

    result = import_csv_to_canonical_rows(input_csv)

    assert result["row_count"] == 2
    assert result["unmapped_headers"] == ["Spare Column"]

    first_row = result["canonical_rows"][0]
    second_row = result["canonical_rows"][1]
    assert first_row["Artist"] == "Act One"
    assert first_row["Primary_Email"] == ""
    assert first_row["Source_URL"] == "https://example.com"
    assert second_row["Artist"] == "Act Two"
    assert second_row["Primary_Email"] == ""
    assert second_row["Source_URL"] == ""
    assert second_row["All_Emails"] == ""


def test_import_preserves_new_outreach_columns_in_canonical_rows(tmp_path) -> None:
    input_csv = tmp_path / "outreach_fields.csv"
    input_csv.write_text(
        "Artist Name,Sounds Like,Social Link,Unearthed_Genre_Raw,Email Type\n"
        "Act One,Flume; Rufus,https://instagram.com/actone,Electronic,email_list\n",
        encoding="utf-8",
    )

    result = import_csv_to_canonical_rows(input_csv)

    row = result["canonical_rows"][0]
    assert row["Artist"] == "Act One"
    assert row["Sounds Like"] == "Flume; Rufus"
    assert row["Social Link"] == "https://instagram.com/actone"
    assert row["Unearthed_Genre_Raw"] == "Electronic"
    assert row["Email_Type"] == "email_list"
    assert row["Email_Source_Type"] == ""


def test_utf8_sig_input_reads_correctly(tmp_path) -> None:
    input_csv = tmp_path / "utf8_sig.csv"
    input_csv.write_text("Artist Name,Email\nSig Act,sig@example.com\n", encoding="utf-8-sig")

    result = import_csv_to_canonical_rows(input_csv)

    assert result["encoding"] == "utf-8-sig"
    assert result["detected_headers"] == ["Artist Name", "Email"]
    assert result["canonical_rows"][0]["Artist"] == "Sig Act"
    assert result["canonical_rows"][0]["Primary_Email"] == "sig@example.com"


def test_duplicate_mapped_headers_use_first_non_empty_value_in_header_order() -> None:
    raw_row = {
        "Primary Email": "",
        "Email": "later@example.com",
    }
    header_map = {
        "Primary Email": "Primary_Email",
        "Email": "Primary_Email",
    }

    canonical_row = build_canonical_row(raw_row, header_map, header_order=["Primary Email", "Email"])

    assert canonical_row["Primary_Email"] == "later@example.com"


def test_duplicate_mapped_headers_do_not_overwrite_first_non_empty_value() -> None:
    raw_row = {
        "Email": "first@example.com",
        "Primary Email": "second@example.com",
    }
    header_map = {
        "Email": "Primary_Email",
        "Primary Email": "Primary_Email",
    }

    canonical_row = build_canonical_row(raw_row, header_map, header_order=["Email", "Primary Email"])

    assert canonical_row["Primary_Email"] == "first@example.com"


def test_duplicate_mapped_headers_follow_detected_input_header_order(tmp_path) -> None:
    input_csv = tmp_path / "duplicate_headers.csv"
    input_csv.write_text(
        "Primary Email,Email\n"
        "first@example.com,second@example.com\n",
        encoding="utf-8",
    )

    result = import_csv_to_canonical_rows(input_csv)

    assert result["detected_headers"] == ["Primary Email", "Email"]
    assert result["canonical_rows"][0]["Primary_Email"] == "first@example.com"
