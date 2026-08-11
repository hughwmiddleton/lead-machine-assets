import json

import pytest

from lead_engine import (
    HumanDecisionType,
    HumanIdentityDecision,
    IdentityClassification,
    IdentityEvidenceRegistry,
    assertion_id,
    compare_identity_rows,
    identity_profile_from_row,
    lead_record_from_row,
    source_occurrence_from_row,
)


def _spotify(artist="The Example", spotify_id="Spotify123", **extra):
    return {
        "Artist Name": artist,
        "Source Directory": "spotify",
        "Spotify Artist ID": spotify_id,
        **extra,
    }


def _bandcamp(artist="The Example", host="the-example", **extra):
    return {
        "Artist Name": artist,
        "Source Directory": "bandcamp",
        "Source URL": f"https://{host}.bandcamp.com/album/debut",
        **extra,
    }


def test_assertion_id_is_order_independent_and_ignores_mutable_evaluation_data():
    left = "le:source-occurrence:v1:test:" + "a" * 64
    right = "le:source-occurrence:v1:test:" + "b" * 64
    assert assertion_id(left, right) == assertion_id(right, left)
    assert assertion_id(left, right).startswith("le:identity-assertion:v1:")


def test_same_spotify_identity_is_exact_and_order_independent():
    left = _spotify(Location="Melbourne", Email="old@example.com")
    right = _spotify(Location="Sydney", Email="new@example.com", final_status="PASS")
    forward = compare_identity_rows(left, right)
    reverse = compare_identity_rows(right, left)
    assert forward == reverse
    assert forward.classification == IdentityClassification.EXACT
    assert forward.auto_join_eligible is True
    assert "same_spotify_artist_id" in forward.reason_codes


@pytest.mark.parametrize(
    "row",
    [
        {
            "Artist Name": "Provider Artist",
            "Source Directory": "bandcamp",
            "Source URL": "https://provider-artist.bandcamp.com/album/one",
        },
        {
            "Artist Name": "Provider Artist",
            "Source Directory": "soundcloud",
            "Source URL": "https://soundcloud.com/providerartist/track-one",
        },
        {
            "Artist Name": "Provider Artist",
            "Source Directory": "Triple J Unearthed",
            "Source URL": "https://www.abc.net.au/triplejunearthed/artist/provider-artist",
        },
        {
            "Artist Name": "Provider Artist",
            "Source Directory": "lastfm",
            "Source URL": "https://www.last.fm/music/Provider+Artist",
        },
    ],
)
def test_equivalent_supported_provider_identity_is_exact(row):
    assertion = compare_identity_rows(row, dict(row))
    assert assertion.classification == IdentityClassification.EXACT


def test_same_name_only_is_heuristic_and_never_auto_joins():
    assertion = compare_identity_rows(_spotify(), _bandcamp())
    assert assertion.classification == IdentityClassification.HEURISTIC
    assert assertion.auto_join_eligible is False
    assert assertion.reason_codes == ("same_artist_name",)


def test_same_name_and_location_remains_non_canonical():
    assertion = compare_identity_rows(
        _spotify(Location="Melbourne"),
        _bandcamp(Location=" melbourne "),
    )
    assert assertion.classification == IdentityClassification.HEURISTIC
    assert assertion.auto_join_eligible is False
    assert "same_location" in assertion.reason_codes
    assert "canonical_entity_id" not in assertion.to_dict()


def test_same_name_with_different_location_does_not_become_strong():
    assertion = compare_identity_rows(_spotify(Location="Melbourne"), _bandcamp(Location="London"))
    assert assertion.classification == IdentityClassification.HEURISTIC
    assert assertion.auto_join_eligible is False
    assert "location_conflict" in assertion.conflict_indicators


def test_independent_social_and_artist_domain_corroboration_is_high_confidence():
    shared = {
        "Instagram_URL": "https://instagram.com/theexample",
        "Website": "https://theexample.com",
        "Domain_Role": "artist_controlled",
    }
    assertion = compare_identity_rows(_spotify(**shared), _bandcamp(**shared))
    assert assertion.classification == IdentityClassification.HIGH_CONFIDENCE
    assert assertion.auto_join_eligible is True
    assert {"same_artist_name", "same_instagram_handle", "same_artist_domain"} <= set(assertion.reason_codes)


def test_duplicated_social_evidence_does_not_inflate_to_high_confidence():
    shared = {
        "Instagram_URL": "https://instagram.com/theexample",
        "Instagram_Handle": "@theexample",
        "External Links": "https://instagram.com/theexample | https://instagram.com/theexample/",
    }
    assertion = compare_identity_rows(_spotify(**shared), _bandcamp(**shared))
    assert assertion.classification == IdentityClassification.HEURISTIC
    assert [item.kind for item in assertion.evidence].count("instagram_handle") == 1


def test_generic_shared_email_does_not_establish_identity():
    shared = {"Email": "bookings@agency.example"}
    assertion = compare_identity_rows(
        _spotify(artist="Artist One", **shared),
        _bandcamp(artist="Artist Two", **shared),
    )
    assert assertion.classification == IdentityClassification.INSUFFICIENT
    assert assertion.auto_join_eligible is False
    assert "same_shared_email" in assertion.reason_codes


def test_direct_email_strength_requires_explicit_provenance_and_independent_corroboration():
    social = {"Instagram_URL": "https://instagram.com/theexample", "Email": "artist@theexample.com"}
    generic = compare_identity_rows(_spotify(**social), _bandcamp(**social))
    direct_fields = {**social, "Contact_Role": "artist", "Contact_Type": "direct"}
    direct = compare_identity_rows(_spotify(**direct_fields), _bandcamp(**direct_fields))
    assert generic.classification == IdentityClassification.HEURISTIC
    assert "same_shared_email" in generic.reason_codes
    assert direct.classification == IdentityClassification.HIGH_CONFIDENCE
    assert "same_direct_email" in direct.reason_codes


def test_conflicting_provider_identity_is_not_hidden_by_matching_name():
    assertion = compare_identity_rows(_spotify(spotify_id="SpotifyA"), _spotify(spotify_id="SpotifyB"))
    assert assertion.classification == IdentityClassification.CONFLICT
    assert assertion.auto_join_eligible is False
    assert "different_spotify_artist_id" in assertion.conflict_indicators
    assert "same_artist_name" in assertion.reason_codes


def test_conflicting_artist_controlled_domains_are_surfaced():
    left = _spotify(Website="https://artist-one.example", Domain_Role="artist")
    right = _bandcamp(Website="https://artist-two.example", Domain_Role="artist")
    assertion = compare_identity_rows(left, right)
    assert assertion.classification == IdentityClassification.CONFLICT
    assert "different_artist_domain" in assertion.conflict_indicators


def test_missing_shared_evidence_is_insufficient():
    assertion = compare_identity_rows(
        _spotify(artist="Artist One"),
        _bandcamp(artist="Artist Two"),
    )
    assert assertion.classification == IdentityClassification.INSUFFICIENT
    assert assertion.reason_codes == ()


def test_human_confirmed_different_overrides_high_confidence_auto_join():
    shared = {
        "Instagram_URL": "https://instagram.com/theexample",
        "Website": "https://theexample.com",
        "Domain_Role": "artist",
    }
    automated = compare_identity_rows(_spotify(**shared), _bandcamp(**shared))
    decision = HumanIdentityDecision(
        automated.assertion_id,
        HumanDecisionType.CONFIRMED_DIFFERENT,
        reason="Known same-name collision",
        actor_id="reviewer-7",
        decided_at="2026-08-11T12:00:00+10:00",
    )
    reviewed = compare_identity_rows(_spotify(**shared), _bandcamp(**shared), human_decision=decision)
    assert reviewed.classification == IdentityClassification.HIGH_CONFIDENCE
    assert reviewed.effective_outcome == "CONFIRMED_DIFFERENT"
    assert reviewed.auto_join_eligible is False
    assert reviewed.human_decision == decision


def test_human_confirmed_same_overrides_advisory_uncertainty_and_preserves_provenance():
    automated = compare_identity_rows(_spotify(), _bandcamp())
    decision = HumanIdentityDecision(
        automated.assertion_id,
        HumanDecisionType.CONFIRMED_SAME,
        reason="Artist confirmed both profiles",
        actor_id="reviewer-9",
        decided_at="2026-08-11T12:30:00+10:00",
    )
    reviewed = compare_identity_rows(_spotify(), _bandcamp(), human_decision=decision)
    assert reviewed.classification == IdentityClassification.HEURISTIC
    assert reviewed.effective_outcome == "CONFIRMED_SAME"
    assert reviewed.auto_join_eligible is True
    assert reviewed.to_dict()["human_decision"]["reason"] == "Artist confirmed both profiles"


def test_unresolved_decision_clears_override_to_automated_policy():
    assertion = compare_identity_rows(_spotify(), _bandcamp())
    unresolved = HumanIdentityDecision(assertion.assertion_id, HumanDecisionType.UNRESOLVED, reason="Re-opened")
    reviewed = compare_identity_rows(_spotify(), _bandcamp(), human_decision=unresolved)
    assert reviewed.effective_outcome == "UNRESOLVED"
    assert reviewed.auto_join_eligible is False


def test_registry_serialization_is_deterministic_across_insertion_order():
    first = compare_identity_rows(_spotify(), _bandcamp())
    second = compare_identity_rows(_spotify(artist="Other", spotify_id="Other1"), _bandcamp(artist="Other", host="other"))
    registry_a = IdentityEvidenceRegistry()
    registry_b = IdentityEvidenceRegistry()
    for assertion in (first, second):
        registry_a.record(assertion)
    for assertion in (second, first):
        registry_b.record(assertion)
    assert registry_a.to_json() == registry_b.to_json()
    assert json.loads(registry_a.to_json())["assertions"]


def test_registry_human_decision_updates_outcome_without_changing_assertion_identity():
    registry = IdentityEvidenceRegistry()
    assertion = registry.record(compare_identity_rows(_spotify(), _bandcamp()))
    decision = HumanIdentityDecision(
        assertion.assertion_id,
        HumanDecisionType.CONFIRMED_DIFFERENT,
        reason="Manual review",
        actor_id="reviewer-1",
    )
    updated = registry.set_human_decision(decision)
    assert updated.assertion_id == assertion.assertion_id
    assert updated.effective_outcome == "CONFIRMED_DIFFERENT"
    assert registry.get(assertion.assertion_id) == updated


def test_pairwise_registry_does_not_materialize_transitive_entities():
    shared = {
        "Artist Name": "Chain Artist",
        "Instagram_URL": "https://instagram.com/chainartist",
        "Website": "https://chainartist.example",
        "Domain_Role": "artist",
    }
    occurrence_a = identity_profile_from_row(
        {**shared, "Source Directory": "spotify", "Spotify Artist ID": "SpotifyA"}
    )
    occurrence_b = identity_profile_from_row(
        {**shared, "Source Directory": "bandcamp", "Source URL": "https://chainartist.bandcamp.com"}
    )
    occurrence_c = identity_profile_from_row(
        {**shared, "Source Directory": "spotify", "Spotify Artist ID": "SpotifyC"}
    )
    registry = IdentityEvidenceRegistry()
    ab = registry.compare_and_record(occurrence_a, occurrence_b)
    bc = registry.compare_and_record(occurrence_b, occurrence_c)
    ac = registry.compare_and_record(occurrence_a, occurrence_c)
    assert ab.classification == IdentityClassification.HIGH_CONFIDENCE
    assert bc.classification == IdentityClassification.HIGH_CONFIDENCE
    assert ac.classification == IdentityClassification.CONFLICT
    assert len(registry.assertions()) == 3
    assert "canonical_entity" not in registry.to_json()


def test_foundation_identifiers_remain_unchanged_and_no_canonical_id_is_generated():
    spotify = _spotify(artist="Stable Artist", spotify_id="Stable123")
    occurrence = source_occurrence_from_row(spotify)
    lead = lead_record_from_row(spotify)
    assert occurrence.source_occurrence_id == (
        "le:source-occurrence:v1:spotify:ba1d42c900f28e39225e29a2244544ac67a242082b06133a464e066bf925299f"
    )
    assert lead.lead_id == "le:lead:v1:b3bf8cf64687cc02c4619571f55ee915df0bae55a83b57d0466c881a006a1250"
    assertion = compare_identity_rows(spotify, spotify)
    assert "canonical_entity_id" not in assertion.to_dict()
