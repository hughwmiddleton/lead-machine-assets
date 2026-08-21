"""
Regression tests for Ticket 4: Harden cross-directory artist identity validation.

These tests verify that:
- Exact and strong identity matches are still accepted
- Weak or unrelated candidates are rejected
- Management/company accounts do not receive false certainty
- Corroboration and contradiction handling work correctly
- Score semantics are conservative and traceable
"""

import pytest
import cross_directory_enricher as cde
from cross_directory_enricher import (
    _compute_identity_match_score,
    _identity_name_tier,
    _identity_handle_tier,
    _identity_contradiction_penalty,
    _identity_corroboration_boost,
    _IDENTITY_PLAUSIBLE_THRESHOLD,
    compute_match_score,
    _sc_score_candidate,
    _bandcamp_confidence,
    _lastfm_confidence,
)


# ---------------------------------------------------------------------------
# 1. Exact normalized artist name remains accepted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "seed,candidate",
    [
        ("Agent Double", "Agent Double"),
        ("Eric Dollois", "Eric Dollois"),
        ("The Nightlights", "The Nightlights"),
        ("Binta", "Binta"),
    ],
)
def test_exact_name_match_is_exact_tier(seed, candidate):
    score, classification, debug = _compute_identity_match_score(seed, candidate, "")
    assert classification == "exact"
    assert score >= 0.95


# ---------------------------------------------------------------------------
# 2. Punctuation/spacing/slug variation remains accepted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "seed,candidate,handle",
    [
        ("Agent Double", "agentdouble", "agentdouble"),
        ("Eric Dollois", "ericdollois", "ericdollois"),
        ("Agent Double", "Agent-Double", "agent-double"),
        ("Eric Dollois", "eric dollois", "eric_dollois"),
    ],
)
def test_slug_and_spacing_variants_accepted(seed, candidate, handle):
    score, classification, debug = _compute_identity_match_score(seed, candidate, handle)
    assert classification in ("exact", "strong")
    assert score >= 0.80

    sc = _sc_score_candidate(seed, candidate, handle)
    assert sc >= 0.80


# ---------------------------------------------------------------------------
# 3. Unrelated username cannot receive strong confidence
# ---------------------------------------------------------------------------
def test_unrelated_username_rejected():
    score, classification, debug = _compute_identity_match_score(
        "Jeremy Ross", "judahblood", "judahblood"
    )
    assert classification == "weak/reject"
    assert score < 0.30

    sc = _sc_score_candidate("Jeremy Ross", "judahblood", "judahblood")
    assert sc < 0.30


# ---------------------------------------------------------------------------
# 4. Incompatible display name overrides weak username similarity
# ---------------------------------------------------------------------------
def test_incompatible_display_name_dominates():
    # Handle is unrelated; display name is also unrelated
    score, classification, debug = _compute_identity_match_score(
        "Jeremy Ross", "judahblood", "judahblood"
    )
    assert classification == "weak/reject"
    assert debug["display_score"] < 0.30
    assert debug["handle_score"] < 0.30


# ---------------------------------------------------------------------------
# 5. Partial token matches do not saturate confidence
# ---------------------------------------------------------------------------
def test_partial_token_match_does_not_saturate():
    # "Smith" is a common token; sharing it should not produce high confidence
    score, classification, debug = _compute_identity_match_score(
        "John Smith", "Smith Records", "smithrecords"
    )
    assert classification == "weak/reject"
    assert score < 0.60

    sc = _sc_score_candidate("John Smith", "Smith Records", "smithrecords")
    assert sc < 0.60


# ---------------------------------------------------------------------------
# 6. Generic tokens do not create false positives
# ---------------------------------------------------------------------------
def test_generic_tokens_do_not_create_false_positives():
    score, classification, debug = _compute_identity_match_score(
        "Music", "Music Official", "musicofficial"
    )
    # Generic seed names are inherently ambiguous; score should remain moderate at best
    assert score < 0.90


# ---------------------------------------------------------------------------
# 7. Location corroboration can strengthen an already plausible name match
# ---------------------------------------------------------------------------
def test_location_corroboration_strengthens_plausible():
    base_score, base_class, _ = _compute_identity_match_score(
        "Agent Double", "Agent Double Project", ""
    )
    assert base_class == "strong"

    boosted_score, boosted_class, debug = _compute_identity_match_score(
        "Agent Double",
        "Agent Double Project",
        "",
        seed_location="Melbourne",
        candidate_location="Melbourne, Australia",
    )
    assert boosted_score > base_score
    assert debug["corroboration"] > 0.0


# ---------------------------------------------------------------------------
# 8. Location alone cannot rescue an incompatible name
# ---------------------------------------------------------------------------
def test_location_alone_cannot_rescue_incompatible_name():
    score, classification, debug = _compute_identity_match_score(
        "Jeremy Ross",
        "judahblood",
        "judahblood",
        seed_location="Melbourne, Australia",
        candidate_location="Melbourne",
    )
    assert classification == "weak/reject"
    assert score < 0.30


# ---------------------------------------------------------------------------
# 9. Direct shared website/domain can strongly corroborate a candidate
# ---------------------------------------------------------------------------
def test_shared_domain_corroborates():
    score, classification, debug = _compute_identity_match_score(
        "Agent Double",
        "Agent Double Project",
        "",
        seed_website="https://agentdouble.com",
        candidate_websites={"https://agentdouble.com/about"},
    )
    assert debug["corroboration"] >= 0.10
    assert score >= 0.80


# ---------------------------------------------------------------------------
# 10. Contradictory website identity reduces/rejects match
# ---------------------------------------------------------------------------
def test_contradictory_website_penalty():
    # Management token in candidate name
    score, classification, debug = _compute_identity_match_score(
        "Black Orange",
        "blackorangemgmt",
        "blackorangemgmt",
    )
    assert debug["contradictions"] >= 0.30
    assert classification == "weak/reject"


# ---------------------------------------------------------------------------
# 11. Missing optional metadata does not automatically reject a strong exact-name match
# ---------------------------------------------------------------------------
def test_missing_metadata_does_not_reject_exact():
    score, classification, debug = _compute_identity_match_score(
        "Agent Double", "Agent Double", "", seed_location="", seed_genre="", seed_website=""
    )
    assert classification == "exact"
    assert score >= 0.95


# ---------------------------------------------------------------------------
# 12. Ambiguous best/runner-up candidates are skipped where appropriate
# ---------------------------------------------------------------------------
def test_ambiguous_candidates_low_confidence():
    # Two candidates with weak similarity; both should score low
    s1, c1, _ = _compute_identity_match_score("John Smith", "Johnny Smith", "")
    s2, c2, _ = _compute_identity_match_score("John Smith", "John Smithers", "")
    assert c1 in ("plausible", "weak/reject")
    assert c2 in ("plausible", "weak/reject")
    assert s1 < 0.80
    assert s2 < 0.80


# ---------------------------------------------------------------------------
# 13. Jeremy Ross -> judahblood regression is rejected
# ---------------------------------------------------------------------------
def test_jeremy_ross_judahblood_regression_rejected():
    score, classification, debug = _compute_identity_match_score(
        "Jeremy Ross", "judahblood", "judahblood"
    )
    assert classification == "weak/reject"
    assert score < 0.20

    sc = _sc_score_candidate("Jeremy Ross", "judahblood", "judahblood")
    assert sc < 0.20

    ms = compute_match_score("Jeremy Ross", "", "judahblood", "", "", "")
    assert ms < 0.30


# ---------------------------------------------------------------------------
# 14. Black Orange -> blackorangemgmt does not receive false certainty
# ---------------------------------------------------------------------------
def test_black_orange_management_regression_rejected():
    score, classification, debug = _compute_identity_match_score(
        "Black Orange", "blackorangemgmt", "blackorangemgmt"
    )
    assert classification == "weak/reject"
    assert score < 0.30
    assert debug["contradictions"] >= 0.30

    sc = _sc_score_candidate("Black Orange", "blackorangemgmt", "blackorangemgmt")
    assert sc < 0.30

    ms = compute_match_score("Black Orange", "", "blackorangemgmt", "", "", "")
    assert ms < 0.30


# ---------------------------------------------------------------------------
# 15. Existing known-good matching tests remain passing
# ---------------------------------------------------------------------------
def test_known_good_bandcamp_confidence():
    # Exact Bandcamp match should still be high
    score = _bandcamp_confidence("Nightlight", "Nightlight", "https://nightlight.bandcamp.com/")
    assert score >= cde.MIN_BC_CONFIDENCE

    # Management label should be penalised
    score = _bandcamp_confidence(
        "Black Orange", "Black Orange Mgmt", "https://blackorangemgmt.bandcamp.com/"
    )
    assert score < cde.MIN_BC_CONFIDENCE


def test_known_good_lastfm_confidence():
    score = _lastfm_confidence("Nightlight", "Nightlight")
    assert score >= 0.95

    score = _lastfm_confidence("Jeremy Ross", "judahblood")
    assert score < 0.30


def test_known_good_soundcloud_confidence():
    score = _sc_score_candidate("Agent Double", "Agent Double", "agentdouble")
    assert score >= 0.95

    score = _sc_score_candidate("Eric Dollois", "Eric Dollois", "ericdollois")
    assert score >= 0.95


# ---------------------------------------------------------------------------
# Debug/semantics tests
# ---------------------------------------------------------------------------
def test_debug_dict_contains_expected_keys():
    score, classification, debug = _compute_identity_match_score(
        "Agent Double", "Agent Double", "agentdouble"
    )
    assert "display_score" in debug
    assert "handle_score" in debug
    assert "base_score" in debug
    assert "contradictions" in debug
    assert "corroboration" in debug
    assert "final" in debug


def test_weak_evidence_never_produces_exact_classification():
    score, classification, debug = _compute_identity_match_score(
        "Jeremy Ross", "judahblood", "judahblood"
    )
    assert classification == "weak/reject"
    assert score < _IDENTITY_PLAUSIBLE_THRESHOLD


def test_management_token_in_handle_penalised():
    # Exact display name but management handle: penalised but remains strong
    score, classification, debug = _compute_identity_match_score(
        "Black Orange", "Black Orange", "blackorangemgmt"
    )
    assert debug["contradictions"] >= 0.10
    assert classification == "strong"
    assert score >= 0.80
