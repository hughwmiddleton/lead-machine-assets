import csv

from lead_vault.importer import ensure_master_csv_exists
from lead_vault.schema import get_canonical_master_schema, get_default_master_csv_path


EXPECTED_CANONICAL_MASTER_SCHEMA = [
    "Artist",
    "Artist_ID",
    "Contact_Name",
    "Contact_Role",
    "Contact_Type",
    "Organization",
    "Organization_Type",
    "Location",
    "City",
    "State",
    "Country",
    "Primary_Genre",
    "Secondary_Genre",
    "Song_Title",
    "Release_Date",
    "Career_Stage",
    "Primary_Email",
    "All_Emails",
    "Phone",
    "Website",
    "Domain",
    "Domain_Root",
    "Contact_Page_URL",
    "Facebook_URL",
    "Instagram_URL",
    "Instagram_Handle",
    "Twitter_URL",
    "SoundCloud_URL",
    "Bandcamp_URL",
    "Spotify_URL",
    "Spotify_Artist_ID",
    "LastFM_URL",
    "YouTube_URL",
    "TikTok_URL",
    "External_Links",
    "Played_On_Triple_J",
    "Played_On_Community_Radio",
    "Played_On_Unearthed",
    "Unearthed_Status",
    "Industry_Signals",
    "Source_Directory",
    "Discovery_Source",
    "Source_URL",
    "Import_Source_File",
    "Import_Batch",
    "Source_Job",
    "Date_Added",
    "First_Discovered_Date",
    "Last_Updated",
    "Last_Enriched",
    "Enrichment_Status",
    "Email_Source",
    "Email_Source_URL",
    "Email_Source_Type",
    "Email_Extract_Method",
    "Contact_Mode",
    "Domain_Type",
    "Domain_Organization",
    "Domain_Role",
    "Confidence",
    "Lead_Score",
    "Needs_Review",
    "Review_Reason",
    "Review_Urls",
    "Suspect_Email",
    "Suspect_Email_All",
    "Data_Quality_Score",
    "Lead_Status",
    "Outreach_Status",
    "Last_Contacted",
    "Followup_Count",
    "Final_Status",
    "Notes",
]


def test_canonical_schema_order_is_stable() -> None:
    assert get_canonical_master_schema() == EXPECTED_CANONICAL_MASTER_SCHEMA


def test_default_master_csv_path_uses_data_directory() -> None:
    path = get_default_master_csv_path()
    assert path.parts[-2:] == ("data", "master_lead_machine_contacts.csv")


def test_master_csv_auto_creates_with_exact_headers(tmp_path) -> None:
    master_path = tmp_path / "nested" / "master.csv"

    ensure_master_csv_exists(master_path)

    assert master_path.exists()
    with open(master_path, "r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        assert next(reader) == EXPECTED_CANONICAL_MASTER_SCHEMA


def test_master_csv_creation_is_idempotent(tmp_path) -> None:
    master_path = tmp_path / "master.csv"

    ensure_master_csv_exists(master_path)
    before = master_path.read_text(encoding="utf-8-sig")

    ensure_master_csv_exists(master_path)
    after = master_path.read_text(encoding="utf-8-sig")

    assert before == after
