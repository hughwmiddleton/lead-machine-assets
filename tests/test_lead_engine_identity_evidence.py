import pandas as pd

from lead_engine import (
    identity_profile_from_contracts,
    identity_profile_from_row,
    lead_record_from_row,
    source_occurrence_from_row,
)


def test_profile_extracts_supported_evidence_families():
    profile = identity_profile_from_row(
        {
            "Artist Name": "Signal Artist",
            "Location": "Melbourne",
            "Source Directory": "spotify",
            "Spotify Artist ID": "Signal123",
            "Instagram_URL": "https://instagram.com/signalartist/",
            "Facebook_URL": "https://www.facebook.com/signalartist",
            "Website": "https://www.signalartist.com/about",
            "Domain_Role": "artist_controlled",
            "Email": "hello@signalartist.com",
            "Contact_Role": "artist",
            "Contact_Type": "direct",
        }
    )
    assert {signal.family for signal in profile.signals} == {"provider", "social", "website", "contact", "context"}
    assert {signal.kind for signal in profile.signals} >= {
        "spotify_artist_id",
        "instagram_handle",
        "facebook_profile",
        "artist_domain",
        "direct_email",
        "artist_name",
        "location",
    }


def test_repeated_social_copies_are_one_underlying_signal():
    profile = identity_profile_from_row(
        {
            "Artist Name": "Duplicate Links",
            "Source Directory": "spotify",
            "Spotify Artist ID": "Duplicate123",
            "Instagram_URL": "https://instagram.com/duplicatelinks",
            "Instagram_Handle": "@duplicatelinks",
            "External Links": "https://instagram.com/duplicatelinks/ | https://instagram.com/duplicatelinks",
        }
    )
    instagram = [signal for signal in profile.signals if signal.kind == "instagram_handle"]
    assert len(instagram) == 1
    assert instagram[0].independence_key == "social:instagram_handle"


def test_link_hub_is_not_treated_as_artist_controlled_domain():
    profile = identity_profile_from_row(
        {
            "Artist Name": "Hub Artist",
            "Source Directory": "spotify",
            "Spotify Artist ID": "Hub123",
            "Website": "https://linktr.ee/hubartist",
            "Domain_Role": "artist_controlled",
        }
    )
    assert not any(signal.family == "website" for signal in profile.signals)


def test_email_is_direct_only_with_explicit_role_and_type_provenance():
    base = {
        "Artist Name": "Email Artist",
        "Source Directory": "spotify",
        "Spotify Artist ID": "Email123",
        "Email": "hello@emailartist.com",
    }
    generic = identity_profile_from_row(base)
    direct = identity_profile_from_row({**base, "Contact_Role": "artist", "Contact_Type": "direct"})
    assert any(signal.kind == "shared_or_unclassified_email" for signal in generic.signals)
    assert any(signal.kind == "direct_email" for signal in direct.signals)


def test_profile_accepts_pandas_series_and_serializes_deterministically():
    profile = identity_profile_from_row(
        pd.Series(
            {
                "Artist Name": "Series Evidence",
                "Source Directory": "bandcamp",
                "Source URL": "https://series-evidence.bandcamp.com",
                "Location": pd.NA,
            }
        )
    )
    assert profile.to_json() == profile.to_json()
    assert not any(signal.kind == "location" for signal in profile.signals)


def test_generic_native_ids_are_namespaced_by_provider():
    partner_a = identity_profile_from_row(
        {
            "Artist Name": "Partner Artist",
            "Source Directory": "partner_a",
            "Source Native ID": "42",
        }
    )
    partner_b = identity_profile_from_row(
        {
            "Artist Name": "Partner Artist",
            "Source Directory": "partner_b",
            "Source Native ID": "99",
        }
    )
    assert {signal.kind for signal in partner_a.signals if signal.family == "provider"} == {
        "partner_a_source_native_id"
    }
    assert {signal.kind for signal in partner_b.signals if signal.family == "provider"} == {
        "partner_b_source_native_id"
    }


def test_profile_reuses_established_source_occurrence_and_lead_record_contracts():
    row = {
        "Artist Name": "Contract Artist",
        "Location": "Melbourne",
        "Source Directory": "spotify",
        "Spotify Artist ID": "Contract123",
        "Email": "contract@example.com",
    }
    occurrence = source_occurrence_from_row(row)
    lead = lead_record_from_row(row)
    profile = identity_profile_from_contracts(occurrence, lead)
    assert profile.source_occurrence is occurrence
    assert {signal.kind for signal in profile.signals} >= {
        "spotify_artist_id",
        "artist_name",
        "location",
        "shared_or_unclassified_email",
    }
