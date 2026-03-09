import pandas as pd
import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde


def _make_worker(logs):
    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    worker.progress = SimpleNamespace(emit=lambda *args, **kwargs: None)
    worker._set_platform_state = lambda *args, **kwargs: None
    return worker


def _record_profile(worker, domain_norm, artist, contacts, source_url="https://example.test/contact", source_type="website_enrich"):
    return worker._record_domain_profile_observation(
        domain_norm,
        artist=artist,
        contacts=list(contacts),
        source_type=source_type,
        source_url=source_url,
    )


def test_domain_profile_accumulates_repeated_same_domain_observations():
    logs = []
    worker = _make_worker(logs)
    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Bright One",
                "Email": "mgmt@brightmusic.com",
                "Email_All": "mgmt@brightmusic.com;friend@other.com",
                "Email_Source_URL": "https://brightmusic.com/contact",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "regex",
                "Email_Type": "website_enrich",
                "Spotify_Website_URL": "https://brightmusic.com/about",
            },
            {
                "Artist Name": "Bright Two",
                "Email": "bookings@brightmusic.com",
                "Email_All": "bookings@brightmusic.com;ally@outside.test",
                "Email_Source_URL": "https://brightmusic.com/about",
                "Email_Source_Type": "facebook_enrich",
                "Email_Extract_Method": "regex",
                "Email_Type": "fb_enrich",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            },
        ],
        dtype=str,
    ).fillna("")

    assert worker._index_domain_email_reuse_from_row(seed_df, 0, "brightmusic.com") is True
    assert worker._index_domain_email_reuse_from_row(seed_df, 1, "brightmusic.com") is False

    assert worker._domain_email_reuse_index["brightmusic.com"]["email"] == "mgmt@brightmusic.com"
    profile = worker._domain_profile_index["brightmusic.com"]
    assert profile["seen_count"] == 2
    assert profile["contacts"] == ["mgmt@brightmusic.com", "bookings@brightmusic.com"]
    assert profile["contact_counts"] == {
        "mgmt@brightmusic.com": 1,
        "bookings@brightmusic.com": 1,
    }
    assert profile["artist_count"] == 2
    assert profile["artists_sample"] == ["Bright One", "Bright Two"]
    assert profile["source_types"] == ["website_enrich", "facebook_enrich"]
    assert profile["first_source_url"] == "https://brightmusic.com/contact"
    assert profile["org_type"] == "unknown"


def test_domain_profile_records_successful_reuse_without_changing_reuse_semantics():
    logs = []
    worker = _make_worker(logs)
    assert worker._index_domain_email_reuse(
        "brightmusic.com",
        email="mgmt@brightmusic.com",
        email_all="mgmt@brightmusic.com",
        source_url="https://www.facebook.com/brightone",
        source_type="facebook_enrich",
        extract_method="regex",
        email_type="fb_enrich",
    )

    seed_df = pd.DataFrame(
        [
            {
                "Artist Name": "Bright Two",
                "Email": "",
                "Email_All": "",
                "Email_Type": "",
                "Email_Source_URL": "",
                "Email_Source_Type": "",
                "Email_Extract_Method": "",
                "Spotify_Website_URL": "https://brightmusic.com/contact",
            }
        ],
        dtype=str,
    ).fillna("")

    ctx = worker._build_row_context(seed_df, 0, 1, 1)

    assert worker._maybe_apply_domain_email_reuse(seed_df, 0, ctx) is True
    assert seed_df.at[0, "Email"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_All"] == "mgmt@brightmusic.com"
    assert seed_df.at[0, "Email_Type"] == "fb_enrich"
    assert seed_df.at[0, "Email_Source_URL"] == "https://www.facebook.com/brightone"
    assert seed_df.at[0, "Email_Source_Type"] == "facebook_enrich"
    assert seed_df.at[0, "Email_Extract_Method"] == "regex"
    assert worker._domain_email_reuse_index["brightmusic.com"]["email"] == "mgmt@brightmusic.com"
    assert worker._domain_email_reuse_count == 1

    profile = worker._domain_profile_index["brightmusic.com"]
    assert profile["seen_count"] == 1
    assert profile["contacts"] == ["mgmt@brightmusic.com"]
    assert profile["contact_counts"] == {"mgmt@brightmusic.com": 1}
    assert profile["artist_count"] == 1
    assert profile["artists_sample"] == ["Bright Two"]
    assert profile["source_types"] == ["facebook_enrich"]
    assert profile["first_source_url"] == "https://www.facebook.com/brightone"
    assert profile["org_type"] == "unknown"
    assert "org_type" not in worker._domain_email_reuse_index["brightmusic.com"]


def test_domain_profile_org_type_management_requires_repeated_artists():
    worker = _make_worker([])

    assert _record_profile(worker, "brightmusic.com", "Bright One", ["mgmt@brightmusic.com"])
    assert _record_profile(worker, "brightmusic.com", "Bright Two", ["management@brightmusic.com"])

    assert worker._domain_profile_index["brightmusic.com"]["org_type"] == "management"


def test_domain_profile_org_type_booking_agency_requires_repeated_artists():
    worker = _make_worker([])

    assert _record_profile(worker, "brightshows.com", "Bright One", ["bookings@brightshows.com"])
    assert _record_profile(worker, "brightshows.com", "Bright Two", ["agent@brightshows.com"])

    assert worker._domain_profile_index["brightshows.com"]["org_type"] == "booking_agency"


def test_domain_profile_org_type_label_from_strong_local_part_clues():
    worker = _make_worker([])

    assert _record_profile(worker, "northrecords.com", "North One", ["releases@northrecords.com"])
    assert _record_profile(worker, "northrecords.com", "North Two", ["info@northrecords.com"])

    assert worker._domain_profile_index["northrecords.com"]["org_type"] == "label"


def test_domain_profile_org_type_mixed_management_and_booking_stays_unknown():
    worker = _make_worker([])

    assert _record_profile(worker, "brightmusic.com", "Bright One", ["mgmt@brightmusic.com"])
    assert _record_profile(worker, "brightmusic.com", "Bright Two", ["bookings@brightmusic.com"])

    assert worker._domain_profile_index["brightmusic.com"]["org_type"] == "unknown"


def test_domain_profile_org_type_single_artist_stays_unknown():
    worker = _make_worker([])

    assert _record_profile(worker, "brightmusic.com", "Bright One", ["mgmt@brightmusic.com"])

    assert worker._domain_profile_index["brightmusic.com"]["org_type"] == "unknown"
