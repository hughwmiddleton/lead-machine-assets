import json

import pandas as pd

from email_normalizer import filter_system_telemetry_emails
from email_provenance import EMAIL_PROVENANCE_JSON_COL
import night_mode_fb
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


def test_set_email_all_prefers_outreach_addresses_in_order():
    df = pd.DataFrame([{"Artist Name": "Artist D", "Email_All": ""}])

    merged = pipeline_runner._set_email_all(
        df,
        0,
        ["support@bandcamp.com", "booking@artist.com", "press@artistlabel.com"],
        source="test_rank",
    )

    assert merged == "booking@artist.com;press@artistlabel.com;support@bandcamp.com"
    assert df.at[0, "Email_All"] == "booking@artist.com;press@artistlabel.com;support@bandcamp.com"


def test_consolidate_email_all_prefers_direct_fb_email_over_external_contact_site():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist E",
                "Spotify_Website_URL": "https://artist.test",
                "Email": "",
                "Email_All": "to@nomograph.mastering;admin@artist.test",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "to@nomograph.mastering": {
                            "source_type": "website_enrich",
                            "surface": "website_contact_page",
                            "source_url": "https://nomograph.mastering/contact",
                            "extract_method": "regex",
                        },
                        "admin@artist.test": {
                            "source_type": "facebook_enrich",
                            "surface": "facebook_about",
                            "source_url": "https://www.facebook.com/artist/about",
                            "extract_method": "regex",
                        },
                    }
                ),
                "Email_Source_URL": "https://nomograph.mastering/contact",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "regex",
            }
        ]
    )

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == "admin@artist.test"
    assert consolidated.at[0, "Email_All"] == "admin@artist.test;to@nomograph.mastering"
    assert consolidated.at[0, "Email_Source_URL"] == "https://www.facebook.com/artist/about"
    assert consolidated.at[0, "Email_Source_Type"] == "facebook_enrich"


def test_consolidate_email_all_keeps_valid_email_when_extractor_filters_fragments():
    extracted, _ = night_mode_fb._extract_emails_from_html(
        "<html><body>and@cultartists.may admin@artist.test +@lover.wav</body></html>"
    )
    df = pd.DataFrame([{"Artist Name": "Artist E2", "Email": "", "Email_All": ";".join(extracted)}])

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert extracted == ["admin@artist.test"]
    assert consolidated.at[0, "Email"] == "admin@artist.test"
    assert consolidated.at[0, "Email_All"] == "admin@artist.test"


def test_consolidate_email_all_prefers_artist_domain_generic_inbox_over_unrelated_external_domain():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist F",
                "Spotify_Website_URL": "https://artist.test",
                "Email": "",
                "Email_All": "bookings@label.test;admin@artist.test",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "bookings@label.test": {
                            "source_type": "soundcloud_live",
                            "surface": "soundcloud_profile",
                            "source_url": "https://soundcloud.com/artist",
                            "extract_method": "regex",
                        },
                        "admin@artist.test": {
                            "source_type": "soundcloud_live",
                            "surface": "soundcloud_profile",
                            "source_url": "https://soundcloud.com/artist",
                            "extract_method": "regex",
                        },
                    }
                ),
            }
        ]
    )

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == "admin@artist.test"
    assert consolidated.at[0, "Email_All"] == "admin@artist.test;bookings@label.test"


def test_consolidate_email_all_keeps_only_service_email_when_only_option():
    df = pd.DataFrame([{"Artist Name": "Artist G", "Email": "", "Email_All": "to@nomograph.mastering"}])

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == "to@nomograph.mastering"
    assert consolidated.at[0, "Email_All"] == "to@nomograph.mastering"


def test_consolidate_email_all_is_deterministic_for_same_artist_domain():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist H",
                "Spotify_Website_URL": "https://artist.test",
                "Email": "",
                "Email_All": "info@artist.test;bookings@artist.test",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "info@artist.test": {
                            "source_type": "website_enrich",
                            "surface": "website_homepage",
                            "source_url": "https://artist.test",
                            "extract_method": "mailto",
                        },
                        "bookings@artist.test": {
                            "source_type": "website_enrich",
                            "surface": "website_homepage",
                            "source_url": "https://artist.test",
                            "extract_method": "mailto",
                        },
                    }
                ),
            }
        ]
    )

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == "bookings@artist.test"
    assert consolidated.at[0, "Email_All"] == "bookings@artist.test;info@artist.test"


def test_consolidate_email_all_keeps_legacy_selected_email_without_support_field():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artist I",
                "Email": "hello@artist.test",
                "Email_All": "contact@artist.test;hello@artist.test",
                "Email_Source_URL": "https://artist.test/contact",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "regex",
            }
        ]
    )

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == "hello@artist.test"


def test_select_primary_email_prefers_identity_matched_email_when_provenance_is_equal():
    row = {
        "Artist Name": "Jazzy Dale",
        "SoundCloud Link": "https://soundcloud.com/jazzypdale",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "for@faridani.co": {
                    "source_type": "soundcloud_live",
                    "surface": "soundcloud_profile",
                    "source_url": "https://soundcloud.com/jazzypdale",
                    "extract_method": "regex",
                },
                "jazzypdale@gmail.com": {
                    "source_type": "soundcloud_live",
                    "surface": "soundcloud_profile",
                    "source_url": "https://soundcloud.com/jazzypdale",
                    "extract_method": "regex",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        "",
        "for@faridani.co;jazzypdale@gmail.com",
    )

    assert primary == "jazzypdale@gmail.com"
    assert ranked == ["jazzypdale@gmail.com", "for@faridani.co"]


def test_select_primary_email_prefers_artist_like_gmail_over_unrelated_domain_with_same_provenance():
    row = {
        "Artist Name": "Artistname Music",
        "SoundCloud Link": "https://soundcloud.com/artistname",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "artistname.music@gmail.com": {
                    "source_type": "soundcloud_live",
                    "surface": "soundcloud_profile",
                    "source_url": "https://soundcloud.com/artistname",
                    "extract_method": "regex",
                },
                "bookings@label.test": {
                    "source_type": "soundcloud_live",
                    "surface": "soundcloud_profile",
                    "source_url": "https://soundcloud.com/artistname",
                    "extract_method": "regex",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        "",
        "artistname.music@gmail.com;bookings@label.test",
    )

    assert primary == "artistname.music@gmail.com"
    assert ranked == ["artistname.music@gmail.com", "bookings@label.test"]


def test_select_primary_email_keeps_artist_domain_preference_over_artist_like_gmail():
    row = {
        "Artist Name": "Artistname",
        "Spotify_Website_URL": "https://artistdomain.com",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "admin@artistdomain.com": {
                    "source_type": "soundcloud_live",
                    "surface": "soundcloud_profile",
                    "source_url": "https://soundcloud.com/artistname",
                    "extract_method": "regex",
                },
                "artistname@gmail.com": {
                    "source_type": "soundcloud_live",
                    "surface": "soundcloud_profile",
                    "source_url": "https://soundcloud.com/artistname",
                    "extract_method": "regex",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        "",
        "admin@artistdomain.com;artistname@gmail.com",
    )

    assert primary == "admin@artistdomain.com"
    assert ranked == ["admin@artistdomain.com", "artistname@gmail.com"]


def test_select_primary_email_keeps_only_third_party_email_when_it_is_the_only_option():
    primary, ranked = pipeline_runner._select_primary_email_for_row(
        {"Artist Name": "Artistname"},
        "",
        "bookings@label.test",
    )

    assert primary == "bookings@label.test"
    assert ranked == ["bookings@label.test"]


def test_select_primary_email_leaves_deterministic_fallback_unchanged_without_identity_signal():
    primary, ranked = pipeline_runner._select_primary_email_for_row(
        {},
        "",
        "zeta@alpha.test;omega@beta.test",
    )

    assert primary == "omega@beta.test"
    assert ranked == ["omega@beta.test", "zeta@alpha.test"]


def test_select_primary_email_remains_deterministic_when_multiple_emails_match_identity():
    row = {
        "Artist Name": "Jazzy Dale",
        "SoundCloud Link": "https://soundcloud.com/jazzydale",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "jazzydale@gmail.com": {
                    "source_type": "soundcloud_live",
                    "surface": "soundcloud_profile",
                    "source_url": "https://soundcloud.com/jazzydale",
                    "extract_method": "regex",
                },
                "jazzydale.music@gmail.com": {
                    "source_type": "soundcloud_live",
                    "surface": "soundcloud_profile",
                    "source_url": "https://soundcloud.com/jazzydale",
                    "extract_method": "regex",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        "",
        "jazzydale@gmail.com;jazzydale.music@gmail.com",
    )

    assert primary == "jazzydale.music@gmail.com"
    assert ranked == ["jazzydale.music@gmail.com", "jazzydale@gmail.com"]


def test_consolidate_email_all_promotes_facebook_email_over_website_primary():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artistname",
                "Spotify_Website_URL": "https://artistname.test",
                "Email": "contact@bandsite.com",
                "Email_All": "contact@bandsite.com;artistname@gmail.com",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "contact@bandsite.com": {
                            "source_type": "website_enrich",
                            "surface": "website_contact_page",
                            "source_url": "https://artistname.test/contact",
                            "extract_method": "regex",
                        },
                        "artistname@gmail.com": {
                            "source_type": "facebook_enrich",
                            "surface": "facebook_about",
                            "source_url": "https://www.facebook.com/artistname/about",
                            "extract_method": "regex",
                        },
                    }
                ),
                "Email_Source_URL": "https://artistname.test/contact",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "regex",
            }
        ]
    )

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == "artistname@gmail.com"
    assert consolidated.at[0, "Email_All"] == "artistname@gmail.com;contact@bandsite.com"
    assert consolidated.at[0, "Email_Source_Type"] == "facebook_enrich"
    assert consolidated.at[0, "Email_Source_URL"] == "https://www.facebook.com/artistname/about"


def test_select_primary_email_keeps_facebook_primary_when_website_email_is_added_later():
    row = {
        "Artist Name": "Artistname",
        "Spotify_Website_URL": "https://artistname.test",
        "Email": "artistname@gmail.com",
        "Email_All": "artistname@gmail.com;contact@bandsite.com",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "artistname@gmail.com": {
                    "source_type": "facebook_enrich",
                    "surface": "facebook_about",
                    "source_url": "https://www.facebook.com/artistname/about",
                    "extract_method": "regex",
                },
                "contact@bandsite.com": {
                    "source_type": "website_enrich",
                    "surface": "website_contact_page",
                    "source_url": "https://artistname.test/contact",
                    "extract_method": "regex",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        row["Email"],
        row["Email_All"],
    )

    assert primary == "artistname@gmail.com"
    assert ranked == ["artistname@gmail.com", "contact@bandsite.com"]


def test_select_primary_email_preserves_existing_behaviour_with_same_trust_candidates():
    row = {
        "Artist Name": "Artistname",
        "Spotify_Website_URL": "https://artistname.test",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "info@artistname.test": {
                    "source_type": "website_enrich",
                    "surface": "website_homepage",
                    "source_url": "https://artistname.test",
                    "extract_method": "mailto",
                },
                "bookings@artistname.test": {
                    "source_type": "website_enrich",
                    "surface": "website_homepage",
                    "source_url": "https://artistname.test",
                    "extract_method": "mailto",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        "",
        "info@artistname.test;bookings@artistname.test",
    )

    assert primary == "bookings@artistname.test"
    assert ranked == ["bookings@artistname.test", "info@artistname.test"]


def test_select_primary_email_prefers_visible_facebook_email_over_mailto_candidate():
    row = {
        "Artist Name": "Artistname",
        "Spotify_Website_URL": "https://artistname.test",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "team@artistname.test": {
                    "source_type": "facebook_enrich",
                    "surface": "facebook_main",
                    "source_url": "https://www.facebook.com/artistname",
                    "extract_method": "mailto",
                },
                "crew@artistname.test": {
                    "source_type": "facebook_enrich",
                    "surface": "facebook_main",
                    "source_url": "https://www.facebook.com/artistname",
                    "extract_method": "regex",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        "",
        "team@artistname.test;crew@artistname.test",
    )

    assert primary == "crew@artistname.test"
    assert ranked == ["crew@artistname.test", "team@artistname.test"]


def test_select_primary_email_keeps_mailto_when_it_is_the_only_candidate():
    row = {
        "Artist Name": "Artistname",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "hello@artistname.test": {
                    "source_type": "facebook_enrich",
                    "surface": "facebook_main",
                    "source_url": "https://www.facebook.com/artistname",
                    "extract_method": "mailto",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        "",
        "hello@artistname.test",
    )

    assert primary == "hello@artistname.test"
    assert ranked == ["hello@artistname.test"]


def test_select_primary_email_keeps_one_hop_candidate_when_it_is_the_only_option():
    row = {
        "Artist Name": "Artistname",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "booking@label.test": {
                    "source_type": "live_search",
                    "surface": "live_search",
                    "source_url": "https://labels.example.com/artistname",
                    "extract_method": "regex",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        "",
        "booking@label.test",
    )

    assert primary == "booking@label.test"
    assert ranked == ["booking@label.test"]


def test_select_primary_email_keeps_direct_profile_email_ahead_of_weaker_external_fallback():
    row = {
        "Artist Name": "Artistname",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(
            {
                "artistname@gmail.com": {
                    "source_type": "instagram_enrich",
                    "surface": "instagram_profile",
                    "source_url": "https://www.instagram.com/artistname/",
                    "extract_method": "regex",
                },
                "booking@label.test": {
                    "source_type": "live_search",
                    "surface": "live_search",
                    "source_url": "https://labels.example.com/artistname",
                    "extract_method": "regex",
                },
            }
        ),
    }

    primary, ranked = pipeline_runner._select_primary_email_for_row(
        row,
        "",
        "booking@label.test;artistname@gmail.com",
    )

    assert primary == "artistname@gmail.com"
    assert ranked == ["artistname@gmail.com", "booking@label.test"]


def test_consolidate_email_all_prefers_facebook_over_placeholder_website_email():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Artistname",
                "Spotify_Website_URL": "https://artistname.test",
                "Email": "user@domain.com",
                "Email_All": "user@domain.com;artistname@gmail.com",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "user@domain.com": {
                            "source_type": "website_enrich",
                            "surface": "website_homepage",
                            "source_url": "https://artistname.test",
                            "extract_method": "regex",
                        },
                        "artistname@gmail.com": {
                            "source_type": "facebook_enrich",
                            "surface": "facebook_about",
                            "source_url": "https://www.facebook.com/artistname/about",
                            "extract_method": "regex",
                        },
                    }
                ),
                "Email_Source_URL": "https://artistname.test",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "regex",
            }
        ]
    )

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == "artistname@gmail.com"
    assert consolidated.at[0, "Email_All"] == "artistname@gmail.com;user@domain.com"


def test_select_primary_email_falls_back_to_existing_order_without_source_metadata():
    primary, ranked = pipeline_runner._select_primary_email_for_row(
        {},
        "",
        "zeta@alpha.test;omega@beta.test",
    )

    assert primary == "omega@beta.test"
    assert ranked == ["omega@beta.test", "zeta@alpha.test"]
