"""Legacy artifacts must not be misread under the 1=consistent polarity.

Artifacts written before commit 17cf863 stored ``name_consistency_flag`` as
1 = *mismatch*.  Artifacts written after it carry
``name_consistency_flag_polarity`` and store 1 = *consistent*.  Detection is by
that explicit marker, never by inspecting the data.

Invariant under test: only an artifact carrying the polarity marker can drive a
name-consistency-based promotion.
"""

import csv
import os

import pandas as pd
import pytest

import final_checker
from final_checker import (
    NAME_CONSISTENCY_FLAG_COL,
    NAME_CONSISTENCY_POLARITY_COL,
    NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1,
    read_name_consistency_flag,
)
from pipeline_runner import recompute_final_status_post_enrichment

SOAK_MASTER_FINAL = "overnight_runs/2026-08-28_102151/master_final.csv"
SOAK_MASTER_POST_FB = "overnight_runs/2026-08-28_102151/master_post_fb.csv"


def _require_soak_artifacts() -> None:
    missing = [path for path in (SOAK_MASTER_FINAL, SOAK_MASTER_POST_FB) if not os.path.isfile(path)]
    if missing:
        pytest.skip(
            "historical ignored soak artifacts unavailable: " + ", ".join(missing)
        )


# --------------------------------------------------------------------------
# The canonical reader
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored, expected",
    [(1, 1), ("1", 1), (1.0, 1), ("1.0", 1), (0, 0), ("0", 0), (0.0, 0), ("0.0", 0)],
)
def test_current_artifact_values_are_used_as_is(stored, expected) -> None:
    row = {
        NAME_CONSISTENCY_FLAG_COL: stored,
        NAME_CONSISTENCY_POLARITY_COL: NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1,
    }
    assert read_name_consistency_flag(row) == expected


@pytest.mark.parametrize("stored", [1, "1", 1.0, "1.0"])
def test_legacy_one_means_mismatch_and_maps_to_inconsistent(stored) -> None:
    """1 in a legacy artifact meant mismatch, so it must never read as consistent."""
    assert read_name_consistency_flag({NAME_CONSISTENCY_FLAG_COL: stored}) == 0


@pytest.mark.parametrize("stored", [0, "0", 0.0, "0.0"])
def test_legacy_zero_is_ambiguous_and_reported_unknown(stored) -> None:
    """0 could be legacy-consistent or current-inconsistent; refuse to guess."""
    assert read_name_consistency_flag({NAME_CONSISTENCY_FLAG_COL: stored}) is None


@pytest.mark.parametrize(
    "row",
    [
        {},
        {NAME_CONSISTENCY_FLAG_COL: ""},
        {NAME_CONSISTENCY_FLAG_COL: float("nan")},
        {NAME_CONSISTENCY_FLAG_COL: None},
        {NAME_CONSISTENCY_FLAG_COL: "not-a-number"},
        {NAME_CONSISTENCY_FLAG_COL: "", NAME_CONSISTENCY_POLARITY_COL: NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1},
    ],
)
def test_absent_or_unparseable_values_read_unknown(row) -> None:
    assert read_name_consistency_flag(row) is None


def test_unrecognised_marker_is_treated_as_legacy() -> None:
    row = {NAME_CONSISTENCY_FLAG_COL: 1, NAME_CONSISTENCY_POLARITY_COL: "something_else"}
    assert read_name_consistency_flag(row) == 0


def test_marker_matching_is_case_and_whitespace_tolerant() -> None:
    row = {NAME_CONSISTENCY_FLAG_COL: 1, NAME_CONSISTENCY_POLARITY_COL: "  Consistent_Is_1  "}
    assert read_name_consistency_flag(row) == 1


def test_reader_tolerates_non_mapping_input() -> None:
    assert read_name_consistency_flag(None) is None
    assert read_name_consistency_flag("not a row") is None


def test_reader_accepts_a_pandas_series() -> None:
    series = pd.Series(
        {
            NAME_CONSISTENCY_FLAG_COL: "1",
            NAME_CONSISTENCY_POLARITY_COL: NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1,
        }
    )
    assert read_name_consistency_flag(series) == 1


# --------------------------------------------------------------------------
# The writer stamps the marker
# --------------------------------------------------------------------------


def test_run_final_checker_stamps_the_polarity_marker(tmp_path) -> None:
    rows = [
        {
            "Artist Name": "Vivid Moss",
            "Song Title": "Slow Bloom",
            "Email": "booking@vividmoss.com",
            "Source URL": "https://vividmoss.bandcamp.com/",
            "Facebook_URL": "https://www.facebook.com/vividmoss",
        },
        {
            "Artist Name": "Vivid Moss",
            "Song Title": "Slow Bloom",
            "Email": "hi@example.com",
            "Source URL": "https://totallyunrelatedname.bandcamp.com/",
            "Facebook_URL": "https://www.facebook.com/totallyunrelatedname",
        },
    ]
    csv_path = tmp_path / "input.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    checked = pd.read_csv(final_checker.run_final_checker(str(csv_path)))

    assert NAME_CONSISTENCY_POLARITY_COL in checked.columns
    assert set(checked[NAME_CONSISTENCY_POLARITY_COL]) == {NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1}
    # Round-trips through the canonical reader.
    assert read_name_consistency_flag(checked.iloc[0].to_dict()) == 1
    assert read_name_consistency_flag(checked.iloc[1].to_dict()) == 0


def test_checker_output_is_not_flipped_by_reprocessing(tmp_path) -> None:
    """A current artifact re-read by the checker keeps its canonical values."""
    rows = [
        {
            "Artist Name": "Vivid Moss",
            "Song Title": "Slow Bloom",
            "Email": "booking@vividmoss.com",
            "Source URL": "https://vividmoss.bandcamp.com/",
            "Facebook_URL": "https://www.facebook.com/vividmoss",
        }
    ]
    csv_path = tmp_path / "input.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    first = final_checker.run_final_checker(str(csv_path))
    first_df = pd.read_csv(first)
    second_df = pd.read_csv(final_checker.run_final_checker(first))

    assert read_name_consistency_flag(first_df.iloc[0].to_dict()) == 1
    assert read_name_consistency_flag(second_df.iloc[0].to_dict()) == 1


# --------------------------------------------------------------------------
# The safety-critical reader: the late BLOCK repair
# --------------------------------------------------------------------------


def _repair_row(**overrides) -> dict:
    row = {
        "Artist Name": "Vivid Moss",
        "final_status": "BLOCK",
        "origin_match_flag": "0",
        "directory_conflict_flag": "1",
        "duplicate_email_flag": "0",
        "duplicate_artist_flag": "0",
        "match_score_overall": "0.75",
        "Email": "booking@vividmoss.com",
        "Email_All": "booking@vividmoss.com",
        "Email_Source_Type": "facebook_enrich",
        "Email_Source_URL": "https://www.facebook.com/vividmoss/about",
        "FB_Status": "found_email",
    }
    row.update(overrides)
    return row


def test_legacy_mismatch_row_is_not_promoted_by_the_repair() -> None:
    """The core hazard: legacy 1 meant mismatch and must stay BLOCK."""
    row = _repair_row(**{NAME_CONSISTENCY_FLAG_COL: "1"})

    result = recompute_final_status_post_enrichment(pd.DataFrame([row]))

    assert result.iloc[0]["final_status"] == "BLOCK"


def test_legacy_zero_row_is_not_promoted_by_the_repair() -> None:
    row = _repair_row(**{NAME_CONSISTENCY_FLAG_COL: "0"})

    result = recompute_final_status_post_enrichment(pd.DataFrame([row]))

    assert result.iloc[0]["final_status"] == "BLOCK"


def test_current_consistent_row_is_still_promoted_by_the_repair() -> None:
    row = _repair_row(
        **{
            NAME_CONSISTENCY_FLAG_COL: "1",
            NAME_CONSISTENCY_POLARITY_COL: NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1,
        }
    )

    result = recompute_final_status_post_enrichment(pd.DataFrame([row]))

    assert result.iloc[0]["final_status"] == "WARN"
    assert str(result.iloc[0]["Needs_Review"]).upper() == "TRUE"


def test_current_inconsistent_row_is_not_promoted_by_the_repair() -> None:
    row = _repair_row(
        **{
            NAME_CONSISTENCY_FLAG_COL: "0",
            NAME_CONSISTENCY_POLARITY_COL: NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1,
        }
    )

    result = recompute_final_status_post_enrichment(pd.DataFrame([row]))

    assert result.iloc[0]["final_status"] == "BLOCK"


def test_no_marker_less_artifact_can_produce_a_promotion() -> None:
    """Exhaustive statement of the invariant across every legacy value."""
    for stored in ("0", "1", "0.0", "1.0", "", "nonsense"):
        row = _repair_row(**{NAME_CONSISTENCY_FLAG_COL: stored})
        result = recompute_final_status_post_enrichment(pd.DataFrame([row]))
        assert result.iloc[0]["final_status"] == "BLOCK", stored


# --------------------------------------------------------------------------
# FB_Name_Consistency_Flag is fail-safe by construction
# --------------------------------------------------------------------------


def _fb_row(**overrides) -> dict:
    row = {
        "Artist Name": "Vivid Moss",
        "Email": "booking@vividmoss.com",
        "Email_All": "booking@vividmoss.com",
        "Email_Source_Type": "facebook_enrich",
        "Email_Source_URL": "https://www.facebook.com/vividmoss/about",
        "FB_Status": "found_email",
        "FB_Review_Reason": "",
    }
    row.update(overrides)
    return row


def _empty_flags() -> dict:
    return {
        "name_flag": 0,
        "dir_conflict_flag": 0,
        "dup_email_flag": 0,
        "dup_artist_flag": 0,
        "genre_outlier_flag": 0,
    }


def test_fb_name_consistency_flag_only_ever_adds_review() -> None:
    """A 0 raises review under either polarity; a 1 is inert.

    So a legacy FB_Name_Consistency_Flag can only ever cost a spurious review,
    never suppress a real one, and needs no polarity migration.
    """
    zero = final_checker.compute_final_status(_fb_row(FB_Name_Consistency_Flag="0"), _empty_flags(), 0.95)
    one = final_checker.compute_final_status(_fb_row(FB_Name_Consistency_Flag="1"), _empty_flags(), 0.95)
    unset = final_checker.compute_final_status(_fb_row(FB_Name_Consistency_Flag=""), _empty_flags(), 0.95)

    assert zero == "WARN"
    assert one == unset == "OK"


# --------------------------------------------------------------------------
# Against the real soak artifacts
# --------------------------------------------------------------------------


def _read_soak(path: str) -> pd.DataFrame:
    _require_soak_artifacts()
    return pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")


@pytest.mark.parametrize("path", [SOAK_MASTER_FINAL, SOAK_MASTER_POST_FB])
def test_soak_artifacts_are_detected_as_legacy(path) -> None:
    df = _read_soak(path)

    assert NAME_CONSISTENCY_FLAG_COL in df.columns
    assert NAME_CONSISTENCY_POLARITY_COL not in df.columns
    assert all(read_name_consistency_flag(df.loc[i].to_dict()) != 1 for i in df.index)


def test_soak_legacy_master_final_yields_no_block_promotions() -> None:
    """Replaying the legacy soak artifact must not lift any row out of BLOCK."""
    df = _read_soak(SOAK_MASTER_FINAL)
    before = df["final_status"].copy()

    after = recompute_final_status_post_enrichment(df.copy())

    lifted = [
        i for i in df.index if before[i] == "BLOCK" and after.at[i, "final_status"] != "BLOCK"
    ]
    assert lifted == []


def test_soak_legacy_promotions_do_not_depend_on_the_flag_value() -> None:
    """Any status change on the legacy soak is independent of the stale flag.

    The WARN -> OK moves come from the genre-rarity policy change, not from
    reading the flag, so perturbing or deleting the column changes nothing.
    """
    base = recompute_final_status_post_enrichment(_read_soak(SOAK_MASTER_FINAL))["final_status"].tolist()

    flipped = _read_soak(SOAK_MASTER_FINAL)
    flipped[NAME_CONSISTENCY_FLAG_COL] = flipped[NAME_CONSISTENCY_FLAG_COL].map({"0": "1", "1": "0"}).fillna("")
    flipped_out = recompute_final_status_post_enrichment(flipped)["final_status"].tolist()

    assert base == flipped_out


def test_dropping_the_flag_column_leaves_statuses_untouched() -> None:
    """Pre-existing gate: the recompute skips rows missing a classifier input."""
    original = _read_soak(SOAK_MASTER_FINAL)
    dropped = original.drop(columns=[NAME_CONSISTENCY_FLAG_COL])

    out = recompute_final_status_post_enrichment(dropped)

    assert out["final_status"].tolist() == original["final_status"].tolist()


def test_soak_block_promotions_return_once_the_artifact_is_re_checked(tmp_path) -> None:
    """The ticket-1 BLOCK -> WARN fixes are withheld on legacy data and restored
    after the checker stamps the polarity marker."""
    legacy = recompute_final_status_post_enrichment(_read_soak(SOAK_MASTER_FINAL))
    legacy_blocks = {i for i in legacy.index if legacy.at[i, "final_status"] == "BLOCK"}

    staged = tmp_path / "master.csv"
    staged.write_bytes(open(SOAK_MASTER_POST_FB, "rb").read())
    rechecked = pd.read_csv(
        final_checker.run_final_checker(str(staged)), dtype=str, keep_default_na=False
    ).fillna("")
    rechecked = recompute_final_status_post_enrichment(rechecked)
    rechecked_blocks = {i for i in rechecked.index if rechecked.at[i, "final_status"] == "BLOCK"}

    # Re-checking lifts the legacy email-bearing BLOCK rows. Current checker
    # policy may also newly block rows with no usable contact, so the exact
    # BLOCK set is not required to be a subset of the historical artifact.
    assert legacy_blocks - rechecked_blocks
    assert len(rechecked_blocks) < len(legacy_blocks)
    assert not any("@" in rechecked.at[i, "Email"] for i in rechecked_blocks - legacy_blocks)
    # No row holding a usable email survives as BLOCK once properly re-checked.
    assert not any("@" in rechecked.at[i, "Email"] for i in rechecked_blocks)


def test_soak_reprocessed_through_checker_is_current_format(tmp_path) -> None:
    """Once re-checked, the soak carries the marker and reads canonically."""
    _require_soak_artifacts()
    staged = tmp_path / "master.csv"
    staged.write_bytes(open(SOAK_MASTER_POST_FB, "rb").read())

    checked = pd.read_csv(final_checker.run_final_checker(str(staged)), dtype=str, keep_default_na=False)

    assert set(checked[NAME_CONSISTENCY_POLARITY_COL]) == {NAME_CONSISTENCY_POLARITY_CONSISTENT_IS_1}
    assert any(read_name_consistency_flag(checked.loc[i].to_dict()) == 1 for i in checked.index)


def test_marker_does_not_leak_into_customer_facing_exports() -> None:
    """The marker is internal: export column lists must not carry it."""
    from pipeline_runner import DEFAULT_EXPORT_COLUMNS, FINAL_EXPORT_COLUMNS

    for columns in (DEFAULT_EXPORT_COLUMNS, FINAL_EXPORT_COLUMNS):
        assert NAME_CONSISTENCY_POLARITY_COL not in columns
        # If the flag itself is ever added to an export, the marker must be too.
        assert NAME_CONSISTENCY_FLAG_COL not in columns


def test_lead_vault_bridge_does_not_promote_a_legacy_master(tmp_path) -> None:
    """The legacy bridge reads arbitrary pre-existing masters; it must not guess."""
    from lead_vault.exporter import FINAL_EXPORT_PRESET, export_with_preset

    input_path = tmp_path / "master.csv"
    output_path = tmp_path / "final_export.csv"
    row = {
        "Artist": "Legacy Mismatch",
        "Primary_Email": "team@legacymismatch.com",
        "All_Emails": "team@legacymismatch.com",
        "Final_Status": "BLOCK",
        "Needs_Review": "",
        "Email_Source_URL": "https://legacymismatch.com/contact",
        "Email_Source_Type": "website_enrich",
        "Email_Extract_Method": "regex",
        "Source_Directory": "website",
        "Source_URL": "https://legacymismatch.com",
        "origin_match_flag": "0",
        "directory_conflict_flag": "0",
        "name_consistency_flag": "1",  # legacy: MISMATCH, no polarity marker
        "duplicate_email_flag": "0",
        "duplicate_artist_flag": "0",
    }
    with open(input_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)

    export_with_preset(FINAL_EXPORT_PRESET, input_path, output_path)

    with open(output_path, "r", encoding="utf-8-sig", newline="") as handle:
        exported = next(csv.DictReader(handle))

    assert exported["final_status"] == "BLOCK"
