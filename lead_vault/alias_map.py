import re
from typing import Dict, Iterable

from .schema import CANONICAL_MASTER_SCHEMA


def normalize_header(header: object) -> str:
    text = "" if header is None else str(header)
    text = text.lstrip("\ufeff").strip().lower()
    text = re.sub(r"[\s\-_]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return text.strip()


_ALIAS_GROUPS = {
    "Artist": [
        "Artist",
        "Artist Name",
        "Band Name",
        "Band",
        "Artist_Name",
    ],
    "Artist_ID": [
        "Artist ID",
        "artist_id",
    ],
    "Contact_Name": [
        "Contact Name",
        "contact_name",
    ],
    "Contact_Role": [
        "Contact Role",
        "contact_role",
    ],
    "Contact_Type": [
        "Contact Type",
        "contact_type",
    ],
    "Organization": [
        "Organization",
        "Company",
        "Label",
    ],
    "Organization_Type": [
        "Organization Type",
        "organization_type",
    ],
    "Location": [
        "Location",
    ],
    "City": [
        "City",
    ],
    "State": [
        "State",
        "Region",
    ],
    "Country": [
        "Country",
        "Country_Derived",
    ],
    "Primary_Genre": [
        "Primary Genre",
        "Genre",
    ],
    "Secondary_Genre": [
        "Secondary Genre",
    ],
    "Song_Title": [
        "Song Title",
        "Track Title",
        "Title",
    ],
    "Release_Date": [
        "Release Date",
    ],
    "Career_Stage": [
        "Career Stage",
    ],
    "Primary_Email": [
        "Primary Email",
        "Email",
        "E-mail",
    ],
    "All_Emails": [
        "All Emails",
        "Email_All",
        "Email All",
        "Emails",
    ],
    "Phone": [
        "Phone",
        "Phone Number",
    ],
    "Website": [
        "Website",
        "Official Website",
        "Spotify Website URL",
    ],
    "Domain": [
        "Domain",
    ],
    "Domain_Root": [
        "Domain Root",
    ],
    "Contact_Page_URL": [
        "Contact Page URL",
        "Contact URL",
    ],
    "Facebook_URL": [
        "Facebook URL",
        "facebook_url",
        "Facebook",
    ],
    "Instagram_URL": [
        "Instagram URL",
        "Instagram",
    ],
    "Instagram_Handle": [
        "Instagram Handle",
    ],
    "Twitter_URL": [
        "Twitter URL",
        "Twitter",
        "X URL",
    ],
    "SoundCloud_URL": [
        "SoundCloud URL",
        "SoundCloud Link",
    ],
    "Bandcamp_URL": [
        "Bandcamp URL",
        "Bandcamp Link",
    ],
    "Spotify_URL": [
        "Spotify URL",
        "Spotify Link",
    ],
    "Spotify_Artist_ID": [
        "Spotify Artist ID",
        "spotify_artist_id",
    ],
    "LastFM_URL": [
        "LastFM URL",
        "Last.fm URL",
    ],
    "YouTube_URL": [
        "YouTube URL",
        "YouTube",
    ],
    "TikTok_URL": [
        "TikTok URL",
        "TikTok",
    ],
    "External_Links": [
        "External Links",
    ],
    "Played_On_Triple_J": [
        "Played On Triple J",
        "Played on triple J",
        "Played on Triple J",
    ],
    "Played_On_Community_Radio": [
        "Played On Community Radio",
        "Played on Community Radio",
    ],
    "Played_On_Unearthed": [
        "Played On Unearthed",
        "Played on Unearthed",
    ],
    "Unearthed_Status": [
        "Unearthed Status",
    ],
    "Industry_Signals": [
        "Industry Signals",
    ],
    "Lead_Source": [
        "Lead Source",
        "Lead_Source",
    ],
    "Source_Directory": [
        "Source Directory",
        "Source_Directory",
        "Source",
    ],
    "Discovery_Source": [
        "Discovery Source",
        "Spotify Playlist",
    ],
    "Source_URL": [
        "Source URL",
        "Source_URL",
        "Source Link",
        "Profile URL",
        "profile_url",
        "artist_url",
        "Artist URL",
    ],
    "Import_Source_File": [
        "Import Source File",
    ],
    "Import_Batch": [
        "Import Batch",
    ],
    "Source_Job": [
        "Source Job",
        "__source_job",
    ],
    "Date_Added": [
        "Date Added",
        "date_added",
    ],
    "First_Discovered_Date": [
        "First Discovered Date",
    ],
    "Last_Updated": [
        "Last Updated",
    ],
    "Last_Enriched": [
        "Last Enriched",
    ],
    "Enrichment_Status": [
        "Enrichment Status",
    ],
    "Email_Source": [
        "Email Source",
    ],
    "Email_Source_URL": [
        "Email Source URL",
    ],
    "Email_Source_Type": [
        "Email Source Type",
    ],
    "Email_Extract_Method": [
        "Email Extract Method",
    ],
    "Email_Provenance_JSON": [
        "Email Provenance JSON",
    ],
    "Contact_Mode": [
        "Contact Mode",
    ],
    "Domain_Type": [
        "Domain Type",
    ],
    "Domain_Organization": [
        "Domain Organization",
    ],
    "Domain_Role": [
        "Domain Role",
    ],
    "Confidence": [
        "Confidence",
    ],
    "Lead_Score": [
        "Lead Score",
    ],
    "Needs_Review": [
        "Needs Review",
    ],
    "Review_Reason": [
        "Review Reason",
        "FB_Review_Reason",
    ],
    "Review_Urls": [
        "Review Urls",
        "Review URLs",
    ],
    "Suspect_Email": [
        "Suspect Email",
    ],
    "Suspect_Email_All": [
        "Suspect Email All",
        "Suspect Email_All",
    ],
    "Data_Quality_Score": [
        "Data Quality Score",
    ],
    "Lead_Status": [
        "Lead Status",
    ],
    "Outreach_Status": [
        "Outreach Status",
    ],
    "Last_Contacted": [
        "Last Contacted",
    ],
    "Followup_Count": [
        "Followup Count",
        "Follow Up Count",
    ],
    "Final_Status": [
        "Final Status",
        "final_status",
    ],
    "Notes": [
        "Notes",
    ],
}


# Current enriched-run implementation details that are useful while a job is
# executing or being diagnosed, but are not durable Lead Vault contact data.
# Keep this explicit so a new, genuinely unknown field still requires review.
_DEFAULT_IGNORED_HEADERS = {
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
}

DEFAULT_IGNORED_HEADER_KEYS = frozenset(normalize_header(header) for header in _DEFAULT_IGNORED_HEADERS)


def is_default_ignored_header(header: object) -> bool:
    return normalize_header(header) in DEFAULT_IGNORED_HEADER_KEYS


def _build_header_aliases() -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for canonical in CANONICAL_MASTER_SCHEMA:
        aliases[normalize_header(canonical)] = canonical
    for canonical, raw_aliases in _ALIAS_GROUPS.items():
        for alias in raw_aliases:
            aliases[normalize_header(alias)] = canonical
    return aliases


HEADER_ALIASES: Dict[str, str] = _build_header_aliases()


def map_headers_to_canonical(headers: Iterable[object]) -> Dict[str, str]:
    mapped: Dict[str, str] = {}
    for header in headers:
        normalized = normalize_header(header)
        canonical = HEADER_ALIASES.get(normalized)
        if canonical:
            mapped[str(header)] = canonical
    return mapped
