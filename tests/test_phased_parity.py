import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

import night_mode_runner
from night_mode_v2.phased_runner import run_contact_phase, run_enrich_phase, run_seed_phase
import pipeline_runner


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")


def _copy_fixture(job_id: str, dest: Path) -> Path:
    fixture_path = FIXTURE_DIR / f"t029_{job_id}_raw.csv"
    df = _load_csv(fixture_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dest, index=False)
    return dest


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    for col in [
        "Artist Name",
        "Song Title",
        "Source URL",
        "Source Directory",
        "Spotify_Artist_ID",
        "Email",
        "Email_All",
        "Suspect_Email_All",
        "External Links",
        "match_score_overall",
        "final_status",
        "Needs_Review",
        "FB_Status",
    ]:
        if col not in work.columns:
            work[col] = ""
    work = work.fillna("")
    return work.sort_values(["Artist Name", "Source URL"], ascending=True).reset_index(drop=True)


def _split_set(val: str, sep: str) -> set[str]:
    if not val:
        return set()
    return {piece.strip() for piece in val.split(sep) if piece.strip()}


def _email_yield(df: pd.DataFrame) -> float:
    total = len(df.index)
    if total == 0:
        return 0.0
    non_empty = df["Email"].astype(str).str.strip().ne("").sum()
    return non_empty / total


def _flag_rate(df: pd.DataFrame, column: str, value: str) -> float:
    total = len(df.index)
    if total == 0:
        return 0.0
    return df[column].astype(str).str.upper().eq(value.upper()).sum() / total


@pytest.fixture
def parity_stubs(monkeypatch, tmp_path):
    # Deterministic stub for raw scraping.
    def fake_run_directory_job(job_config, raw_output_path, logger=None):
        job_id = job_config.get("job_id") or job_config.get("id")
        _copy_fixture(job_id, Path(raw_output_path))
        return raw_output_path

    # Master enrichment: pass-through with gentle canonicalization.
    def fake_run_master_enrichment(input_csv, output_csv, logger=None, enable_live_search=True, max_live_searches=None, night_mode=False):
        df = _load_csv(Path(input_csv))
        if "match_score_overall" not in df.columns:
            df["match_score_overall"] = 0.72
        df.to_csv(output_csv, index=False)
        return output_csv

    # Validation/enrichment: ensure status columns exist and deterministic values.
    def fake_run_enrichment(raw_csv_path, enriched_output_path, logger=None, night_mode=False):
        df = _load_csv(Path(raw_csv_path))
        if "final_status" not in df.columns:
            df["final_status"] = df["Email"].apply(lambda v: "OK" if str(v).strip() else "REVIEW")
        if "Needs_Review" not in df.columns:
            df["Needs_Review"] = df["Email"].apply(lambda v: "" if str(v).strip() else "TRUE")
        if "match_score_overall" not in df.columns:
            df["match_score_overall"] = 0.71
        df.to_csv(enriched_output_path, index=False)
        return enriched_output_path

    # Facebook pass stub.
    def fake_fb_pass(input_csv, output_csv, state_path=None, **kwargs):
        df = _load_csv(Path(input_csv))
        fb_status_col = "FB_Status" if "FB_Status" in df.columns else "FB_Status"
        df[fb_status_col] = df["Email"].apply(lambda v: "ok" if str(v).strip() else "pending")
        # Set one deterministic URL on the first row lacking email.
        no_email_idx = df["Email"].astype(str).str.strip().eq("")
        if no_email_idx.any():
            idx = no_email_idx.idxmax()
            df.loc[idx, "Facebook_URL"] = "https://facebook.com/testpage"
        df.to_csv(output_csv, index=False)
        return pipeline_runner.FacebookGlobalPassStatus(
            processed_rows=len(df.index),
            total_rows=len(df.index),
            completed=True,
            hit_captcha=False,
            limit_reached=False,
            attempted_total=len(df.index),
        )

    def fake_export_master_leads(input_csv, output_csv, logger=None, export_profile=None):
        df = _load_csv(Path(input_csv))
        Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_csv, index=False)

    monkeypatch.setattr(pipeline_runner, "run_directory_job", fake_run_directory_job)
    monkeypatch.setattr(pipeline_runner, "run_master_enrichment", fake_run_master_enrichment)
    monkeypatch.setattr(pipeline_runner, "run_enrichment", fake_run_enrichment)
    monkeypatch.setattr(pipeline_runner, "run_facebook_global_pass_nightmode", fake_fb_pass)
    monkeypatch.setattr(pipeline_runner, "export_master_leads", fake_export_master_leads)

    # Ensure website/email scraping is dormant by disabling live search.
    monkeypatch.setenv("NIGHT_SC_MAX_FETCHES", "0")
    monkeypatch.setenv("NIGHT_SC_BUDGET_SECONDS", "0")

    return tmp_path


def _build_v1_pipeline(tmp_path: Path):
    run_dir = tmp_path / "v1"
    run_dir.mkdir(parents=True, exist_ok=True)

    job_states = []
    for job_id in ("job_a", "job_b"):
        job_dir = run_dir / job_id
        raw_csv = _copy_fixture(job_id, job_dir / "raw.csv")
        job_states.append({"job_id": job_id, "raw_csv": raw_csv.as_posix()})

    logger = night_mode_runner.logging.getLogger("t029_v1")

    master_raw = night_mode_runner._merge_raw_master(run_dir.as_posix(), job_states, logger)
    master_enriched = pipeline_runner.run_master_enrichment(master_raw, (run_dir / "master_enriched.csv").as_posix(), logger=logger.info, enable_live_search=False, max_live_searches=0, night_mode=True)
    master_pre_fb_path = (run_dir / "master_pre_fb.csv").as_posix()
    master_pre_fb = pipeline_runner.run_enrichment(master_enriched, master_pre_fb_path, logger=logger.info, night_mode=True)

    df_pre = _load_csv(Path(master_pre_fb))
    df_pre = night_mode_runner.quarantine_repeated_emails(df_pre, min_repeats=5, logger=logger)
    df_pre.to_csv(master_pre_fb_path, index=False)

    master_post_fb = pipeline_runner.run_facebook_global_pass_nightmode(master_pre_fb_path, (run_dir / "master_post_fb.csv").as_posix(), state_path=(run_dir / "fb_state.json").as_posix(), logger=logger.info)
    master_final = pipeline_runner.run_enrichment((run_dir / "master_post_fb.csv").as_posix(), (run_dir / "master_final.csv").as_posix(), logger=logger.info, night_mode=True)

    v1_df = _load_csv(Path(master_final))
    return run_dir, _normalize_df(v1_df)


def _build_v2_pipeline(tmp_path: Path):
    run_dir = tmp_path / "v2"
    run_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "export_profile": "full_dump",
        "master_enrichment": {"enabled": True, "enable_live_search": False, "max_live_searches": 0},
        "jobs": [
            {"job_id": "job_a", "directory": "spotify", "target_valid_leads": 2},
            {"job_id": "job_b", "directory": "bandcamp", "target_valid_leads": 2},
        ],
    }

    config_path = tmp_path / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    seed_manifest = run_seed_phase(config_path.as_posix(), run_dir.as_posix())
    enrich_manifest = run_enrich_phase(config, run_dir.as_posix(), seed_manifest)
    run_contact_phase(config, run_dir.as_posix(), enrich_manifest)

    final_path = run_dir / "master_final.csv"
    v2_df = _load_csv(final_path)
    return run_dir, _normalize_df(v2_df)


def _assert_parity(v1_df: pd.DataFrame, v2_df: pd.DataFrame):
    assert len(v1_df.index) == len(v2_df.index), f"Row count mismatch: v1={len(v1_df)} v2={len(v2_df)}"

    keys = list(zip(v1_df["Artist Name"], v1_df["Source URL"]))
    keys_v2 = list(zip(v2_df["Artist Name"], v2_df["Source URL"]))
    assert keys == keys_v2, "Row ordering by key differs between v1 and v2"

    exact_cols = ["Artist Name", "Song Title", "Source URL", "Source Directory", "Spotify_Artist_ID"]
    for col in exact_cols:
        if col in v1_df.columns or col in v2_df.columns:
            assert v1_df[col].tolist() == v2_df[col].tolist(), f"Column {col} mismatch"

    set_cols = {"Email_All": ";", "External Links": "|", "Suspect_Email_All": ";"}
    for col, sep in set_cols.items():
        if col in v1_df.columns or col in v2_df.columns:
            for idx in range(len(v1_df.index)):
                v1_set = _split_set(str(v1_df.at[idx, col]), sep)
                v2_set = _split_set(str(v2_df.at[idx, col]), sep)
                assert v1_set == v2_set, f"Set mismatch in {col} at row {idx}: {v1_set} vs {v2_set}"

    if "match_score_overall" in v1_df.columns or "match_score_overall" in v2_df.columns:
        v1_scores = pd.to_numeric(v1_df["match_score_overall"], errors="coerce").fillna(0.0)
        v2_scores = pd.to_numeric(v2_df["match_score_overall"], errors="coerce").fillna(0.0)
        deltas = (v1_scores - v2_scores).abs()
        assert (deltas <= 0.01).all(), f"match_score_overall deltas exceed tolerance: {deltas.tolist()}"

    for col in ["final_status", "Needs_Review", "FB_Status"]:
        if col in v1_df.columns or col in v2_df.columns:
            assert v1_df[col].tolist() == v2_df[col].tolist(), f"Status column {col} mismatch"

    # Non-regression metrics
    v1_email_yield = _email_yield(v1_df)
    v2_email_yield = _email_yield(v2_df)
    assert v2_email_yield + 1e-9 >= v1_email_yield - 0.01, f"Email yield regressed: v1={v1_email_yield:.3f} v2={v2_email_yield:.3f}"

    v1_suspect = _flag_rate(v1_df, "Needs_Review", "TRUE")
    v2_suspect = _flag_rate(v2_df, "Needs_Review", "TRUE")
    assert v2_suspect <= v1_suspect + 0.02 + 1e-9, f"Suspect rate worse: v1={v1_suspect:.3f} v2={v2_suspect:.3f}"

    v1_block = _flag_rate(v1_df, "final_status", "BLOCK")
    v2_block = _flag_rate(v2_df, "final_status", "BLOCK")
    assert v2_block <= v1_block + 0.02 + 1e-9, f"BLOCK rate worse: v1={v1_block:.3f} v2={v2_block:.3f}"


def test_v1_v2_parity(monkeypatch, parity_stubs):
    tmp_path = parity_stubs

    _, v1_df = _build_v1_pipeline(tmp_path)
    _, v2_df = _build_v2_pipeline(tmp_path)

    _assert_parity(v1_df, v2_df)
