import json
from pathlib import Path

import pandas as pd
import pytest
import requests

import cross_directory_enricher as cde
import musicbrainz_identity as mbi
import pipeline_runner
from lead_vault.importer import import_csv_to_canonical_rows


MBID = "11111111-2222-3333-4444-555555555555"
OTHER_MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _url_lookup_payload(*mbids):
    return {
        "resource": "https://open.spotify.com/artist/spotify-id",
        "relations": [
            {"target-type": "artist", "artist": {"id": mbid, "name": f"Artist {index}"}}
            for index, mbid in enumerate(mbids)
        ],
    }


def _artist_payload(mbid=MBID):
    return {
        "id": mbid,
        "name": "Artist A",
        "sort-name": "Artist A",
        "country": "AU",
        "aliases": [{"name": "Artist Alpha", "sort-name": "Artist Alpha", "primary": True}],
        "relations": [
            {
                "target-type": "url",
                "type": "official homepage",
                "url": {"resource": "https://artist.example/"},
            },
            {
                "target-type": "url",
                "type": "bandcamp",
                "url": {"resource": "https://artist-a.bandcamp.com/"},
            },
            {
                "target-type": "url",
                "type": "social network",
                "url": {"resource": "https://www.instagram.com/artist_a/"},
            },
            {
                "target-type": "url",
                "type": "discogs",
                "url": {"resource": "https://www.discogs.com/artist/123"},
            },
        ],
    }


def _client(responses, **overrides):
    session = FakeSession(responses)
    sleeps = []
    max_retries = overrides.pop("max_retries", 0)
    client = mbi.MusicBrainzClient(
        enabled=True,
        user_agent="LeadMachineTests/1.0 (tests@example.com)",
        timeout_seconds=2,
        min_interval_seconds=1,
        max_retries=max_retries,
        session=session,
        sleep_fn=sleeps.append,
        clock_fn=lambda: 100.0,
        **overrides,
    )
    return client, session, sleeps


@pytest.mark.parametrize(
    ("raw", "expected_id"),
    [
        ("https://open.spotify.com/artist/abc123?si=tracking", "abc123"),
        ("https://open.spotify.com/intl-en/artist/abc123/", "abc123"),
        ("http://play.spotify.com/artist/abc123", "abc123"),
        ("spotify:artist:abc123", "abc123"),
    ],
)
def test_spotify_artist_url_variations_normalize(raw, expected_id):
    assert mbi.normalize_spotify_artist_identity("", raw) == (
        expected_id,
        f"https://open.spotify.com/artist/{expected_id}",
    )


def test_spotify_artist_id_is_preferred_and_constructs_canonical_url():
    assert mbi.normalize_spotify_artist_identity(
        "preferred-id",
        "https://open.spotify.com/artist/ignored-id?si=x",
    ) == ("preferred-id", "https://open.spotify.com/artist/preferred-id")


def test_exact_spotify_url_relationship_resolves_unique_artist_mbid():
    client, session, sleeps = _client(
        [FakeResponse(payload=_url_lookup_payload(MBID)), FakeResponse(payload=_artist_payload())]
    )

    result = client.resolve("spotify-id")

    assert result.status == "matched"
    assert result.mbid == MBID
    assert result.match_method == "spotify_url_relationship"
    assert result.identity_confidence == 1.0
    assert session.calls[0][0].endswith("/url")
    assert session.calls[0][1]["params"] == {
        "resource": "https://open.spotify.com/artist/spotify-id",
        "inc": "artist-rels",
        "fmt": "json",
    }
    assert session.calls[1][0].endswith(f"/artist/{MBID}")
    assert session.calls[1][1]["params"] == {"inc": "url-rels+aliases", "fmt": "json"}
    assert result.artist["aliases"] == [
        {"name": "Artist Alpha", "sort-name": "Artist Alpha", "primary": True}
    ]
    assert sleeps == [1.0]


def test_no_exact_relationship_is_neutral_no_match():
    client, session, _ = _client([FakeResponse(status_code=404)])
    result = client.resolve("spotify-id")
    assert result.status == "no_match"
    assert result.mbid == ""
    assert result.identity_confidence is None
    assert len(session.calls) == 1


def test_url_entity_without_artist_relationship_is_no_match():
    client, _, _ = _client([FakeResponse(payload={"relations": []})])
    assert client.resolve("spotify-id").status == "no_match"


def test_multiple_exact_artist_relationships_are_ambiguous():
    client, session, _ = _client([FakeResponse(payload=_url_lookup_payload(MBID, OTHER_MBID))])
    result = client.resolve("spotify-id")
    assert result.status == "ambiguous"
    assert set(result.candidate_mbids) == {MBID, OTHER_MBID}
    assert result.identity_confidence is None
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("response", "error_prefix"),
    [
        (requests.Timeout("slow"), "network:Timeout"),
        (FakeResponse(status_code=500), "http:500"),
        (FakeResponse(payload=ValueError("bad json")), "malformed_json"),
        (FakeResponse(payload=[]), "malformed_payload"),
    ],
)
def test_lookup_failures_are_neutral_errors(response, error_prefix):
    client, _, _ = _client([response])
    result = client.resolve("spotify-id")
    assert result.status == "error"
    assert result.error == error_prefix
    assert result.identity_confidence is None


def test_artist_metadata_api_failure_is_safe_error():
    client, _, _ = _client(
        [FakeResponse(payload=_url_lookup_payload(MBID)), FakeResponse(status_code=503)]
    )
    result = client.resolve("spotify-id")
    assert result.status == "error"
    assert result.mbid == MBID
    assert result.match_method == "spotify_url_relationship"


def test_disabled_client_is_neutral_and_makes_no_request():
    session = FakeSession([])
    client = mbi.MusicBrainzClient(enabled=False, session=session)
    result = client.resolve("spotify-id")
    assert result.status == "disabled"
    assert result.identity_confidence is None
    assert session.calls == []


def test_missing_user_agent_disables_requests():
    session = FakeSession([])
    client = mbi.MusicBrainzClient(enabled=True, user_agent="", session=session)
    assert client.resolve("spotify-id").status == "disabled"
    assert session.calls == []


def test_transient_failure_retries_conservatively():
    client, session, sleeps = _client(
        [
            FakeResponse(status_code=503, headers={"Retry-After": "2"}),
            FakeResponse(payload=_url_lookup_payload()),
        ],
        max_retries=1,
    )
    assert client.resolve("spotify-id").status == "no_match"
    assert len(session.calls) == 2
    assert sleeps == [2.0]


def test_relationships_are_evidence_only_and_not_promoted():
    client, _, _ = _client(
        [FakeResponse(payload=_url_lookup_payload(MBID)), FakeResponse(payload=_artist_payload())]
    )
    original = {
        "Artist Name": "Artist A",
        "Spotify_Artist_ID": "spotify-id",
        "Spotify_URL": "https://open.spotify.com/artist/spotify-id",
        "Bandcamp_URL": "",
        "SoundCloud Link": "",
        "Facebook_URL": "",
        "Instagram_URL": "",
        "Website": "",
        "External Links": "",
    }

    output = mbi.apply_musicbrainz_shadow(pd.DataFrame([original]), client)
    evidence = json.loads(output.at[0, "Identity_Evidence_JSON"])

    assert output.at[0, "MusicBrainz_MBID"] == MBID
    assert output.at[0, "MusicBrainz_Status"] == "matched"
    assert evidence["musicbrainz"]["relationships"]["bandcamp"][0]["url"] == "https://artist-a.bandcamp.com/"
    assert evidence["musicbrainz"]["relationships"]["instagram"][0]["url"] == "https://www.instagram.com/artist_a/"
    for column in ("Bandcamp_URL", "SoundCloud Link", "Facebook_URL", "Instagram_URL", "Website", "External Links"):
        assert output.at[0, column] == original[column]


def test_duplicate_spotify_rows_share_cached_resolution():
    client, session, _ = _client(
        [FakeResponse(payload=_url_lookup_payload(MBID)), FakeResponse(payload=_artist_payload())]
    )
    dataframe = pd.DataFrame(
        [
            {"Artist Name": "Artist A", "Spotify_Artist_ID": "spotify-id"},
            {
                "Artist Name": "Artist A duplicate",
                "Spotify_URL": "https://open.spotify.com/artist/spotify-id?si=duplicate",
            },
        ]
    ).fillna("")

    output = mbi.apply_musicbrainz_shadow(dataframe, client)

    assert list(output["MusicBrainz_Status"]) == ["matched", "matched"]
    assert len(session.calls) == 2


class RecordingClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def resolve(self, artist_id="", artist_url=""):
        self.calls.append((artist_id, artist_url))
        return self.results.pop(0)


def _result(status, confidence=None):
    return mbi.MusicBrainzIdentityResult(
        "spotify-id",
        "https://open.spotify.com/artist/spotify-id",
        status,
        mbid=MBID if status == "matched" else "",
        match_method="spotify_url_relationship" if status == "matched" else "",
        identity_confidence=confidence,
    )


def test_only_spotify_origin_rows_enter_resolver_and_non_spotify_values_remain_unchanged():
    spotify = {
        "Artist Name": "Spotify Artist",
        "Spotify_Artist_ID": "spotify-id",
        "Source Directory": "bandcamp",
    }
    non_spotify = {
        "Artist Name": "Other Artist",
        "Spotify_Artist_ID": "",
        "Spotify_URL": "",
        "Source Directory": "bandcamp",
        "Email": "safe@example.com",
    }
    client = RecordingClient([_result("no_match")])
    output = mbi.apply_musicbrainz_shadow(pd.DataFrame([spotify, non_spotify]).fillna(""), client)

    assert len(client.calls) == 1
    assert output.at[0, "MusicBrainz_Status"] == "no_match"
    assert output.at[1, "MusicBrainz_Status"] == ""
    for key, value in non_spotify.items():
        assert output.at[1, key] == value


def test_shadow_resolution_preserves_all_origin_fields():
    row = {
        "Artist Name": "Artist A",
        "Spotify_Artist_ID": "spotify-id",
        "Lead_Source": "Spotify",
        "Source_Directory": "Spotify",
        "Source Directory": "Spotify",
        "Source URL": "https://open.spotify.com/artist/spotify-id?origin=seed",
    }
    client = RecordingClient([_result("matched", 1.0)])
    output = mbi.apply_musicbrainz_shadow(pd.DataFrame([row]), client)
    for field in ("Lead_Source", "Source_Directory", "Source Directory", "Source URL"):
        assert output.at[0, field] == row[field]


@pytest.mark.parametrize(
    ("status", "confidence", "row"),
    [
        (
            "no_match",
            None,
            {
                "Email": "site@example.com",
                "Email_Source": "website_enrich",
                "Email_Provenance_JSON": '{"site@example.com":{"source_type":"website_enrich"}}',
                "final_status": "valid",
            },
        ),
        (
            "ambiguous",
            None,
            {
                "Email": "artist@example.com",
                "Email_Source": "soundcloud_profile",
                "Email_Provenance_JSON": '{"artist@example.com":{"source_type":"soundcloud_profile"}}',
                "final_status": "valid",
            },
        ),
        ("error", None, {"final_status": "manual_review"}),
        (
            "matched",
            1.0,
            {
                "Email": "unsafe@example.com",
                "Email_Source": "facebook_private_route",
                "final_status": "unsafe",
            },
        ),
    ],
)
def test_identity_outcomes_do_not_change_contact_safety_or_scores(status, confidence, row):
    protected = {
        "final_status": row.get("final_status", ""),
        "Match_Score": "0.42",
        "match_score_overall": "0.37",
        "directory_conflict_flag": "yes",
        "name_consistency_flag": "no",
        "Email": row.get("Email", ""),
        "Email_Source": row.get("Email_Source", ""),
        "Email_Provenance_JSON": row.get("Email_Provenance_JSON", ""),
    }
    input_row = {
        "Artist Name": "Artist A",
        "Spotify_Artist_ID": "spotify-id",
        **protected,
    }
    client = RecordingClient([_result(status, confidence)])
    output = mbi.apply_musicbrainz_shadow(pd.DataFrame([input_row]), client)

    for field, value in protected.items():
        assert output.at[0, field] == value


def test_shadow_fields_round_trip_through_lead_vault_import(tmp_path):
    evidence = json.dumps({"resolver_version": mbi.MUSICBRAINZ_RESOLVER_VERSION})
    path = tmp_path / "musicbrainz.csv"
    pd.DataFrame(
        [
            {
                "Artist": "Artist A",
                "Spotify_Artist_ID": "spotify-id",
                "MusicBrainz_MBID": MBID,
                "MusicBrainz_Status": "matched",
                "Identity_Match_Method": "spotify_url_relationship",
                "Identity_Confidence": "1.0",
                "Identity_Evidence_JSON": evidence,
            }
        ]
    ).to_csv(path, index=False)

    row = import_csv_to_canonical_rows(path)["canonical_rows"][0]

    assert row["MusicBrainz_MBID"] == MBID
    assert row["MusicBrainz_Status"] == "matched"
    assert row["Identity_Match_Method"] == "spotify_url_relationship"
    assert row["Identity_Confidence"] == "1.0"
    assert row["Identity_Evidence_JSON"] == evidence


def test_pipeline_runs_shadow_stage_before_cross_directory_enrichment(tmp_path, monkeypatch):
    seed_path = tmp_path / "master_raw.csv"
    output_path = tmp_path / "master_enriched.csv"
    row = {
        "Artist Name": "Artist A",
        "Spotify_Artist_ID": "spotify-id",
        "Lead_Source": "Spotify",
        "Source_Directory": "Spotify",
        "Source Directory": "Spotify",
        "Source URL": "https://open.spotify.com/artist/spotify-id",
    }
    pd.DataFrame([row]).to_csv(seed_path, index=False)
    events = []
    shadow_paths = []

    def fake_shadow(input_path, shadow_path, logger=None):
        events.append("musicbrainz")
        shadow_paths.append(shadow_path)
        dataframe = pd.read_csv(input_path, dtype=str, keep_default_na=False)
        dataframe["MusicBrainz_Status"] = "matched"
        dataframe.to_csv(shadow_path, index=False)
        return shadow_path

    def fake_cross_directory(input_path, output_path_arg, **kwargs):
        events.append("cross_directory")
        dataframe = pd.read_csv(input_path, dtype=str, keep_default_na=False)
        assert dataframe.at[0, "MusicBrainz_Status"] == "matched"
        dataframe.to_csv(output_path_arg, index=False)
        return output_path_arg

    monkeypatch.setenv("MUSICBRAINZ_SHADOW_ENABLED", "1")
    monkeypatch.setattr(mbi, "run_musicbrainz_shadow_csv", fake_shadow)
    monkeypatch.setattr(cde, "run_cross_directory_enrichment", fake_cross_directory)
    monkeypatch.setattr(pipeline_runner.time, "sleep", lambda _seconds: None)

    pipeline_runner.run_master_enrichment(
        seed_path.as_posix(),
        output_path.as_posix(),
        enable_live_search=False,
    )

    output = pd.read_csv(output_path, dtype=str, keep_default_na=False)
    assert events == ["musicbrainz", "cross_directory"]
    assert output.at[0, "MusicBrainz_Status"] == "matched"
    for field in ("Lead_Source", "Source_Directory", "Source Directory", "Source URL"):
        assert output.at[0, field] == row[field]
    assert shadow_paths and not Path(shadow_paths[0]).exists()


def test_pipeline_shadow_stage_failure_falls_back_to_original_seed(tmp_path, monkeypatch):
    seed_path = tmp_path / "master_raw.csv"
    output_path = tmp_path / "master_enriched.csv"
    original = {
        "Artist Name": "Artist A",
        "Spotify_Artist_ID": "spotify-id",
        "Email": "safe@example.com",
        "final_status": "valid",
    }
    pd.DataFrame([original]).to_csv(seed_path, index=False)
    cross_directory_inputs = []

    def failing_shadow(_input_path, _shadow_path, logger=None):
        raise requests.Timeout("MusicBrainz unavailable")

    def fake_cross_directory(input_path, output_path_arg, **kwargs):
        cross_directory_inputs.append(input_path)
        pd.read_csv(input_path, dtype=str, keep_default_na=False).to_csv(output_path_arg, index=False)
        return output_path_arg

    monkeypatch.setenv("MUSICBRAINZ_SHADOW_ENABLED", "1")
    monkeypatch.setattr(mbi, "run_musicbrainz_shadow_csv", failing_shadow)
    monkeypatch.setattr(cde, "run_cross_directory_enrichment", fake_cross_directory)
    monkeypatch.setattr(pipeline_runner.time, "sleep", lambda _seconds: None)

    pipeline_runner.run_master_enrichment(
        seed_path.as_posix(),
        output_path.as_posix(),
        enable_live_search=False,
    )

    output = pd.read_csv(output_path, dtype=str, keep_default_na=False)
    assert cross_directory_inputs == [seed_path.as_posix()]
    for field, value in original.items():
        assert output.at[0, field] == value
