from copy import deepcopy

import pytest

from lead_engine import InsufficientIdentityEvidence, source_occurrence_from_row


@pytest.mark.parametrize(
    ("row", "expected_kind"),
    [
        (
            {
                "Artist Name": "Hannah Brewer",
                "Source Directory": "Triple J Unearthed",
                "Source URL": "https://www.abc.net.au/triplejunearthed/artist/hannah-brewer",
            },
            "unearthed_artist_slug",
        ),
        (
            {
                "Artist Name": "Signal Artist",
                "Source Directory": "bandcamp/leeds",
                "Source URL": "https://signalartist.bandcamp.com/album/demo",
            },
            "bandcamp_artist_host",
        ),
        (
            {
                "Artist Name": "Signal Artist",
                "Source Directory": "soundcloud",
                "SoundCloud Link": "https://soundcloud.com/signalartist/night-drive",
            },
            "soundcloud_handle",
        ),
        (
            {
                "Artist Name": "Signal Artist",
                "Source Directory": "spotify",
                "Spotify_Artist_ID": "5AbCdEf123456789",
                "Spotify_URL": "https://open.spotify.com/artist/5AbCdEf123456789",
            },
            "spotify_artist_id",
        ),
        (
            {
                "Artist Name": "Sigur Rós",
                "Source Directory": "lastfm_directory",
                "lastfm_url": "https://www.last.fm/music/Sigur+R%C3%B3s",
            },
            "lastfm_artist_path",
        ),
        (
            {
                "Artist Name": "Lunar Echo",
                "Source Directory": "festival_bigsound",
                "Source URL": "https://www.bigsound.org.au/artists",
            },
            "festival_lineup_url_and_artist_name",
        ),
    ],
)
def test_representative_source_identity(row, expected_kind):
    occurrence = source_occurrence_from_row(row)
    assert occurrence.identity_kind == expected_kind
    assert occurrence.source_occurrence_id.startswith("le:source-occurrence:v1:")


def test_cosmetic_differences_do_not_change_safe_platform_identity():
    first = {
        "Artist Name": "  Signal   Artist ",
        "Source Directory": " SOUNDCLOUD ",
        "Source URL": "HTTPS://SOUNDCLOUD.COM/SignalHandle/tracks?utm_source=feed#bio",
    }
    second = {
        "Artist Name": "Signal Artist",
        "Source_Directory": "soundcloud",
        "Source_URL": "https://soundcloud.com/signalhandle/",
    }
    assert source_occurrence_from_row(first).source_occurrence_id == source_occurrence_from_row(second).source_occurrence_id


def test_same_artist_name_with_distinct_spotify_ids_remains_distinct():
    base = {"Artist Name": "Common Name", "Source Directory": "spotify"}
    one = {**base, "Spotify Artist ID": "ABC123"}
    two = {**base, "Spotify Artist ID": "XYZ789"}
    assert source_occurrence_from_row(one).source_occurrence_id != source_occurrence_from_row(two).source_occurrence_id


@pytest.mark.parametrize(
    ("source_directory", "url_key", "first_url", "second_url"),
    [
        (
            "bandcamp",
            "Source URL",
            "https://first-artist.bandcamp.com/album/common-name",
            "https://second-artist.bandcamp.com/album/common-name",
        ),
        (
            "soundcloud",
            "SoundCloud Link",
            "https://soundcloud.com/first-handle/song",
            "https://soundcloud.com/second-handle/song",
        ),
    ],
)
def test_matching_names_do_not_collapse_distinct_platform_profiles(
    source_directory, url_key, first_url, second_url
):
    base = {"Artist Name": "Common Name", "Source Directory": source_directory}
    assert source_occurrence_from_row({**base, url_key: first_url}).source_occurrence_id != source_occurrence_from_row(
        {**base, url_key: second_url}
    ).source_occurrence_id


def test_same_name_in_different_locations_is_only_a_weak_occurrence_fallback():
    base = {"Artist Name": "Common Name", "Source Directory": "local_seed"}
    melbourne = source_occurrence_from_row({**base, "Location": "Melbourne"})
    london = source_occurrence_from_row({**base, "Location": "London"})
    assert melbourne.identity_strength == "weak"
    assert london.identity_strength == "weak"
    assert melbourne.source_occurrence_id != london.source_occurrence_id


def test_same_artist_across_sources_has_distinct_source_occurrences():
    spotify = source_occurrence_from_row(
        {"Artist Name": "One Artist", "Source Directory": "spotify", "Spotify Artist ID": "artist123"}
    )
    soundcloud = source_occurrence_from_row(
        {
            "Artist Name": "One Artist",
            "Source Directory": "soundcloud",
            "SoundCloud Link": "https://soundcloud.com/oneartist",
        }
    )
    assert spotify.source_occurrence_id != soundcloud.source_occurrence_id


def test_missing_native_id_uses_documented_source_url_fallback():
    occurrence = source_occurrence_from_row(
        {
            "Artist Name": "Playlist Curator",
            "Source Directory": "spotify",
            "Source URL": "https://open.spotify.com/user/curator-123",
        }
    )
    assert occurrence.identity_kind == "source_url"
    assert occurrence.identity_value == "https://open.spotify.com/user/curator-123"


def test_explicit_non_spotify_source_native_id_is_preferred():
    occurrence = source_occurrence_from_row(
        {
            "Artist Name": "Native Artist",
            "Source Directory": "directory_partner",
            "Source Native ID": " partner-artist-42 ",
            "Source URL": "https://partner.example/artists/native-artist",
        }
    )
    assert occurrence.identity_kind == "source_native_id"
    assert occurrence.identity_value == "partner-artist-42"
    assert occurrence.source_native_id == " partner-artist-42 "


def test_mutable_enrichment_and_workflow_fields_do_not_change_occurrence_id():
    base = {
        "Artist Name": "Stable Artist",
        "Source Directory": "spotify",
        "Spotify Artist ID": "Stable123",
        "Email": "first@example.com",
        "final_status": "WARN",
        "Needs_Review": "yes",
        "Spotify_Followers": "10",
        "Notes": "before",
    }
    changed = deepcopy(base)
    changed.update(
        {
            "Email": "second@example.com",
            "final_status": "PASS",
            "Needs_Review": "no",
            "Spotify_Followers": "99999",
            "Notes": "after",
        }
    )
    assert source_occurrence_from_row(base).source_occurrence_id == source_occurrence_from_row(changed).source_occurrence_id


def test_malformed_url_produces_explicit_weak_fallback_not_false_native_identity():
    occurrence = source_occurrence_from_row(
        {"Artist Name": "Fallback Artist", "Source Directory": "soundcloud", "Source URL": "not a url"}
    )
    assert occurrence.identity_kind == "artist_location_source_fallback"
    assert occurrence.identity_strength == "weak"


def test_insufficient_identity_evidence_fails_safely():
    with pytest.raises(InsufficientIdentityEvidence):
        source_occurrence_from_row({"Email": "contact@example.com"})
