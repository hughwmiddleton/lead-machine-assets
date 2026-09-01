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


def test_guard_recognizes_all_emails_in_per_email_provenance(monkeypatch):
    messages = []
    monkeypatch.setenv("EMAIL_ALL_GUARD", "1")
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Multi-email Instagram Artist",
                "Email": "primary@artist.com",
                "Email_All": "primary@artist.com;secondary@management.com",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "primary@artist.com": {
                            "source_type": "instagram_enrich",
                            "surface": "instagram_profile",
                            "source_url": "https://www.instagram.com/artist/",
                            "extract_method": "regex",
                        },
                        "secondary@management.com": {
                            "source_type": "instagram_enrich",
                            "surface": "instagram_profile",
                            "source_url": "https://www.instagram.com/artist/",
                            "extract_method": "regex",
                        },
                    }
                ),
            }
        ]
    )

    pipeline_runner._set_email_all(
        df,
        0,
        df.at[0, "Email_All"],
        source="fb_global_pass",
        logger=messages.append,
        provenance_emails=[],
    )

    assert df.at[0, "Email_All"] == "primary@artist.com;secondary@management.com"
    assert not any("email_not_in_sources" in message for message in messages)


def test_guard_recognizes_independent_mixed_source_provenance(monkeypatch):
    messages = []
    monkeypatch.setenv("EMAIL_ALL_GUARD", "1")
    provenance = {
        "onehop@artist.com": {
            "source_type": "instagram_enrich",
            "surface": "instagram_bio_link_one_hop",
            "source_url": "https://artist.com/contact",
            "extract_method": "mailto",
        },
        "booking@agency.com": {
            "source_type": "website_enrich",
            "surface": "website_contact_page",
            "source_url": "https://agency.com/artist",
            "extract_method": "regex",
        },
    }
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Mixed Source Artist",
                "Email": "onehop@artist.com",
                "Email_All": "onehop@artist.com;booking@agency.com",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(provenance),
            }
        ]
    )

    pipeline_runner._set_email_all(
        df,
        0,
        df.at[0, "Email_All"],
        source="fb_global_pass",
        logger=messages.append,
        source_url="https://www.facebook.com/artist",
        source_type="facebook_enrich",
        surface="facebook_main",
        provenance_emails=[],
    )

    assert set(df.at[0, "Email_All"].split(";")) == set(provenance)
    assert json.loads(df.at[0, EMAIL_PROVENANCE_JSON_COL]) == provenance
    assert not any("email_not_in_sources" in message for message in messages)


def test_guard_still_flags_truly_unprovenanced_secondary_email(monkeypatch):
    messages = []
    monkeypatch.setenv("EMAIL_ALL_GUARD", "1")
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Unprovenanced Artist",
                "Email": "primary@artist.com",
                "Email_All": "primary@artist.com;unknown@elsewhere.com",
                EMAIL_PROVENANCE_JSON_COL: json.dumps(
                    {
                        "primary@artist.com": {
                            "source_type": "website_enrich",
                            "surface": "website_homepage",
                            "source_url": "https://artist.com",
                            "extract_method": "regex",
                        }
                    }
                ),
            }
        ]
    )

    pipeline_runner._set_email_all(
        df,
        0,
        df.at[0, "Email_All"],
        source="test_guard",
        logger=messages.append,
        provenance_emails=[],
    )

    assert any("email_not_in_sources='unknown@elsewhere.com'" in message for message in messages)


def test_facebook_no_result_preserves_existing_contacts_and_provenance(monkeypatch):
    messages = []
    monkeypatch.setenv("EMAIL_ALL_GUARD", "1")
    provenance = {
        "anton@agency.com": {
            "source_type": "instagram_enrich",
            "surface": "instagram_bio_link_one_hop",
            "source_url": "https://artist.example/",
            "extract_method": "mailto",
        },
        "secondary@label.com": {
            "source_type": "instagram_enrich",
            "surface": "instagram_bio_link_one_hop",
            "source_url": "https://artist.example/",
            "extract_method": "mailto",
        },
    }
    row = {
        "Artist Name": "Facebook No Result",
        "Email": "anton@agency.com",
        "Email_All": "anton@agency.com;secondary@label.com",
        "Email_Type": "ig_enrich",
        "Email_Source_URL": "https://artist.example/",
        "Email_Source_Type": "instagram_enrich",
        "Email_Extract_Method": "mailto",
        EMAIL_PROVENANCE_JSON_COL: json.dumps(provenance),
    }
    enricher = night_mode_fb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=None,
    )
    no_result = night_mode_fb.NightModeFacebookResult(
        email=None,
        email_all=row["Email_All"],
        email_type="fb_night",
        facebook_url="https://www.facebook.com/artist",
        email_source="main",
        email_source_url="https://www.facebook.com/artist",
        email_extract_method="regex",
    )

    enriched = enricher._apply_night_fb_result(
        dict(row),
        no_result,
        [],
        "https://www.facebook.com/artist",
    )
    df = pd.DataFrame([enriched])
    pipeline_runner._set_email_all(
        df,
        0,
        enriched["Email_All"],
        source="fb_global_pass",
        logger=messages.append,
        source_url=enriched["Email_Source_URL"],
        source_type=enriched["Email_Source_Type"],
        method=enriched["Email_Extract_Method"],
        surface="facebook_main",
        provenance_emails=[],
    )

    assert df.at[0, "Email"] == row["Email"]
    assert df.at[0, "Email_All"] == row["Email_All"]
    assert df.at[0, "Email_Type"] == "ig_enrich"
    assert df.at[0, "Email_Source_URL"] == row["Email_Source_URL"]
    assert df.at[0, "Email_Source_Type"] == "instagram_enrich"
    assert json.loads(df.at[0, EMAIL_PROVENANCE_JSON_COL]) == provenance
    assert "__fb_emails_applied" not in enriched
    assert not any("email_not_in_sources" in message for message in messages)


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
        ["general@artist.com", "booking@artist.com", "press@artistlabel.com"],
        source="test_rank",
    )

    assert merged == "booking@artist.com;press@artistlabel.com;general@artist.com"
    assert df.at[0, "Email_All"] == "booking@artist.com;press@artistlabel.com;general@artist.com"


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


def test_shy_one_primary_email_and_provenance_are_selected_atomically():
    provenance = {
        "info@shyone.co.uk": {
            "source_type": "website_enrich",
            "surface": "website_contact_page",
            "source_url": "https://shyone.co.uk/contact",
            "extract_method": "mailto",
        },
        "abbey@poem.agency": {
            "source_type": "bandcamp",
            "surface": "bandcamp_profile",
            "source_url": "https://shyone.bandcamp.com/",
            "extract_method": "regex",
        },
        "alexandra@higher-ground.de": {
            "source_type": "instagram_enrich",
            "surface": "instagram_profile",
            "source_url": "https://www.instagram.com/shyclart/",
            "extract_method": "regex",
        },
    }
    df = pd.DataFrame(
        [{
            "Artist Name": "Shy One",
            "Spotify_Website_URL": "https://shyone.co.uk/",
            "Email": "abbey@poem.agency",
            "Primary Email": "abbey@poem.agency",
            "Primary_Email": "abbey@poem.agency",
            "Email_All": "alexandra@higher-ground.de;abbey@poem.agency;info@shyone.co.uk",
            EMAIL_PROVENANCE_JSON_COL: json.dumps(provenance),
            "Email_Source_Type": "bandcamp",
            "Email_Source_URL": "https://shyone.bandcamp.com/",
            "Email_Extract_Method": "regex",
            "final_status": "OK",
            "MusicBrainz_MBID": "must-not-leak",
        }],
        index=[41],
    )

    consolidated = pipeline_runner._consolidate_email_all(df)
    winner = consolidated.at[41, "Email"]
    winner_meta = json.loads(consolidated.at[41, EMAIL_PROVENANCE_JSON_COL])[winner]

    assert winner == "alexandra@higher-ground.de"
    assert consolidated.at[41, "Primary Email"] == winner
    assert consolidated.at[41, "Primary_Email"] == winner
    assert consolidated.at[41, "Email_Source_Type"] == winner_meta["source_type"]
    assert consolidated.at[41, "Email_Source_URL"] == winner_meta["source_url"]
    assert consolidated.at[41, "Email_Extract_Method"] == winner_meta["extract_method"]
    assert set(consolidated.at[41, "Email_All"].split(";")) == set(provenance)
    assert set(json.loads(consolidated.at[41, EMAIL_PROVENANCE_JSON_COL])) == set(provenance)

    export_frame = pipeline_runner._build_final_export_frame(consolidated)
    assert export_frame.iloc[0]["Primary Email"] == winner
    assert export_frame.iloc[0]["Email Source"] == "Instagram profile"
    assert export_frame.iloc[0]["Email_Source_Type"] == winner_meta["source_type"]
    assert export_frame.iloc[0]["Email_Source_URL"] == winner_meta["source_url"]
    assert export_frame.iloc[0]["Email_Extract_Method"] == winner_meta["extract_method"]
    assert "MusicBrainz_MBID" not in export_frame.columns


def test_primary_change_without_explicit_provenance_clears_old_source_bundle():
    df = pd.DataFrame([{
        "Artist Name": "Safe Artist",
        "Email": "user@domain.com",
        "Email_All": "user@domain.com;booking@safeartist.example",
        "Email_Source_Type": "instagram_enrich",
        "Email_Source_URL": "https://www.instagram.com/unrelated/",
        "Email_Extract_Method": "regex",
    }])

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == "booking@safeartist.example"
    assert consolidated.at[0, "Email_Source_Type"] == ""
    assert consolidated.at[0, "Email_Source_URL"] == ""
    assert consolidated.at[0, "Email_Extract_Method"] == ""


def test_quarantined_email_cannot_borrow_an_alternate_emails_safe_provenance():
    df = pd.DataFrame([{
        "Artist Name": "Safe Artist",
        "Email": "unsafe@repeated.example",
        "Email_All": "unsafe@repeated.example;booking@safeartist.example",
        "Email Source": "Quarantined (repeat email)",
        EMAIL_PROVENANCE_JSON_COL: json.dumps({
            "booking@safeartist.example": {
                "source_type": "website_enrich",
                "surface": "website_contact_page",
                "source_url": "https://safeartist.example/contact",
                "extract_method": "mailto",
            }
        }),
        "Email_Source_Type": "quarantined",
        "Email_Source_URL": "https://unrelated.example/",
        "Email_Extract_Method": "repeat_email_guard",
    }])

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == "unsafe@repeated.example"
    assert consolidated.at[0, "Email_Source_Type"] == "quarantined"
    assert consolidated.at[0, "Email_Source_URL"] == "https://unrelated.example/"
    assert consolidated.at[0, "Email_Extract_Method"] == "repeat_email_guard"


def test_select_primary_email_falls_back_to_existing_order_without_source_metadata():
    primary, ranked = pipeline_runner._select_primary_email_for_row(
        {},
        "",
        "zeta@alpha.test;omega@beta.test",
    )

    assert primary == "omega@beta.test"
    assert ranked == ["omega@beta.test", "zeta@alpha.test"]


# --- Wix/Sentry telemetry filtering ---

def test_filter_system_telemetry_emails_rejects_wix_sentry():
    assert filter_system_telemetry_emails(["abc@sentry.wixpress.com"]) == []


def test_filter_system_telemetry_emails_rejects_wix_sentry_next():
    assert filter_system_telemetry_emails(["abc@sentry-next.wixpress.com"]) == []


def test_filter_system_telemetry_emails_rejects_wix_sentry_subdomain():
    assert filter_system_telemetry_emails(["abc@sub.sentry.wixpress.com"]) == []


def test_filter_system_telemetry_emails_preserves_legitimate_wix_domain():
    """Artist websites hosted on Wix must not be rejected merely for using Wix."""
    assert filter_system_telemetry_emails(["booking@artist.wixsite.com"]) == ["booking@artist.wixsite.com"]


def test_filter_system_telemetry_emails_preserves_normal_artist_email():
    assert filter_system_telemetry_emails(
        ["contact@artist.com", "abc@sentry.wixpress.com"]
    ) == ["contact@artist.com"]


def test_platform_filter_rejects_any_reverbnation_owned_mailbox():
    assert pipeline_runner.filter_platform_support_emails(
        ["itunes@reverbnation.com", "booking@artists.example"]
    ) == ["booking@artists.example"]


def test_platform_filter_preserves_artist_contact_from_page_with_reverbnation_widget():
    assert pipeline_runner.filter_platform_support_emails(
        ["booking@artist.example"]
    ) == ["booking@artist.example"]


def test_set_email_all_drops_wix_telemetry_only_result():
    df = pd.DataFrame([{"Artist Name": "Artist C", "Email_All": ""}])

    merged = pipeline_runner._set_email_all(
        df,
        0,
        ["abc@sentry.wixpress.com"],
        source="test_filter",
    )

    assert merged == ""
    assert df.at[0, "Email_All"] == ""


def test_set_email_all_drops_wix_telemetry_mixed_with_valid():
    df = pd.DataFrame([{"Artist Name": "Artist D", "Email_All": ""}])

    merged = pipeline_runner._set_email_all(
        df,
        0,
        ["abc@sentry.wixpress.com", "booking@artist.test"],
        source="test_filter",
    )

    assert merged == "booking@artist.test"
    assert df.at[0, "Email_All"] == "booking@artist.test"


def test_consolidate_email_all_blocks_row_with_only_telemetry_email():
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Telemetry Artist",
                "Email": "abc@sentry-next.wixpress.com",
                "Email_All": "abc@sentry-next.wixpress.com",
            }
        ]
    )

    consolidated = pipeline_runner._consolidate_email_all(df)

    assert consolidated.at[0, "Email"] == ""
    assert consolidated.at[0, "Email_All"] == ""
