from types import SimpleNamespace

import pandas as pd
from bs4 import BeautifulSoup

import cross_directory_enricher as cde
import pipeline_runner


def _build_worker(tmp_path):
    worker = cde.CrossDirectoryEnricherWorker(
        seed_csv_path="",
        output_csv_path=(tmp_path / "master_enriched.csv").as_posix(),
        enable_live_search=False,
        max_live_searches=0,
    )
    logs = []
    worker.log_message = SimpleNamespace(emit=lambda msg: logs.append(msg))
    return worker, logs


def test_extract_bandcamp_related_artist_names_from_existing_profile_html():
    html = """
    <div class="recommended-grid-container">
      <div class="collection-item"><div class="item-artist">Solar Harbor</div></div>
      <div class="collection-item"><div class="item-artist">Silver Coast</div></div>
      <div class="collection-item"><div class="item-artist">Midnight Echo</div></div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")

    related = cde._extract_bandcamp_related_artist_names(
        soup,
        "https://atlasbloom.bandcamp.com/",
    )

    assert related == ["Solar Harbor", "Silver Coast", "Midnight Echo"]


def test_stage_festival_expansion_dedupes_self_and_existing_rows(tmp_path):
    worker, logs = _build_worker(tmp_path)
    worker._festival_expansion_existing_keys = {
        cde.normalise_artist_name("Atlas Bloom"),
        cde.normalise_artist_name("Silver Coast"),
    }
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Atlas Bloom",
                "Seed Priority": "festival",
                "Festival Sources": "bigsound",
                "Festival Count": "1",
            }
        ]
    )

    staged = worker._stage_festival_expansion_candidates(
        df,
        0,
        ["Atlas Bloom", "Solar Harbor", "Silver Coast", "Midnight Echo"],
        origin="bandcamp",
    )

    assert staged == 2
    assert [row["Artist Name"] for row in worker._festival_expansion_rows] == [
        "Solar Harbor",
        "Midnight Echo",
    ]
    assert any("skipped_existing=1" in msg for msg in logs)


def test_stage_festival_expansion_row_shape_is_seed_like_and_non_recursive(tmp_path):
    worker, _ = _build_worker(tmp_path)
    worker._festival_expansion_existing_keys = {cde.normalise_artist_name("Atlas Bloom")}
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Atlas Bloom",
                "Seed Priority": "festival_high",
                "Festival Sources": "bigsound;sxsw",
                "Festival Count": "2",
            }
        ]
    )

    staged = worker._stage_festival_expansion_candidates(
        df,
        0,
        ["Solar Harbor"],
        origin="bandcamp",
    )

    assert staged == 1
    row = worker._festival_expansion_rows[0]
    assert row["Expansion Parent"] == "Atlas Bloom"
    assert row["Expansion Origin"] == "bandcamp"
    assert row["Discovery Tier"] == "festival_expansion"
    assert row["Seed Priority"] == ""
    assert row["Source Directory"] == ""
    assert row["Festival Sources"] == "bigsound;sxsw"
    assert row["Festival Count"] == "2"


def test_stage_festival_expansion_respects_per_parent_cap(tmp_path, monkeypatch):
    worker, _ = _build_worker(tmp_path)
    worker._festival_expansion_existing_keys = {cde.normalise_artist_name("Atlas Bloom")}
    df = pd.DataFrame([{"Artist Name": "Atlas Bloom", "Seed Priority": "festival"}])
    monkeypatch.setattr(cde, "FESTIVAL_EXPANSION_MAX_RELATED_ARTISTS", 2)

    staged = worker._stage_festival_expansion_candidates(
        df,
        0,
        ["Solar Harbor", "Silver Coast", "Midnight Echo"],
        origin="bandcamp",
    )

    assert staged == 2
    assert [row["Artist Name"] for row in worker._festival_expansion_rows] == [
        "Solar Harbor",
        "Silver Coast",
    ]


def test_festival_expansion_rows_do_not_expand_again(tmp_path):
    worker, _ = _build_worker(tmp_path)
    worker._festival_expansion_existing_keys = {
        cde.normalise_artist_name("Atlas Bloom"),
        cde.normalise_artist_name("Solar Harbor"),
    }
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Solar Harbor",
                "Discovery Tier": "festival_expansion",
                "Expansion Parent": "Atlas Bloom",
                "Festival Sources": "bigsound",
                "Festival Count": "1",
            }
        ]
    )

    staged = worker._stage_festival_expansion_candidates(
        df,
        0,
        ["Silver Coast"],
        origin="bandcamp",
    )

    assert staged == 0
    assert worker._festival_expansion_rows == []


def test_run_master_enrichment_runs_second_pass_and_merges_expansions(tmp_path, monkeypatch):
    seed_csv = tmp_path / "master_raw.csv"
    output_csv = tmp_path / "master_enriched.csv"
    pd.DataFrame([{"Artist Name": "Atlas Bloom", "Seed Priority": "festival"}]).to_csv(seed_csv, index=False)

    calls = []

    def fake_run_cross_directory_enrichment(seed_path, output_path, **kwargs):
        calls.append((seed_path, output_path))
        if len(calls) == 1:
            pd.DataFrame([{"Artist Name": "Atlas Bloom", "Source Directory": "Bandcamp"}]).to_csv(output_path, index=False)
            pd.DataFrame(
                [
                    {
                        "Artist Name": "Solar Harbor",
                        "Expansion Parent": "Atlas Bloom",
                        "Expansion Origin": "bandcamp",
                        "Discovery Tier": "festival_expansion",
                    }
                ]
            ).to_csv(cde._festival_expansion_raw_path(output_path), index=False)
        else:
            pd.DataFrame(
                [
                    {
                        "Artist Name": "Solar Harbor",
                        "Source Directory": "Bandcamp",
                        "Expansion Parent": "Atlas Bloom",
                        "Expansion Origin": "bandcamp",
                        "Discovery Tier": "festival_expansion",
                    }
                ]
            ).to_csv(output_path, index=False)

    monkeypatch.setattr(cde, "run_cross_directory_enrichment", fake_run_cross_directory_enrichment)

    pipeline_runner.run_master_enrichment(
        seed_csv.as_posix(),
        output_csv.as_posix(),
        logger=None,
        enable_live_search=False,
        bandcamp_csv_path="",
    )

    merged = pd.read_csv(output_csv, dtype=str, keep_default_na=False).fillna("")
    assert len(calls) == 2
    assert list(merged["Artist Name"]) == ["Atlas Bloom", "Solar Harbor"]


def test_run_master_enrichment_skips_second_pass_when_no_expansions(tmp_path, monkeypatch):
    seed_csv = tmp_path / "master_raw.csv"
    output_csv = tmp_path / "master_enriched.csv"
    pd.DataFrame([{"Artist Name": "Atlas Bloom", "Seed Priority": "festival"}]).to_csv(seed_csv, index=False)

    calls = []

    def fake_run_cross_directory_enrichment(seed_path, output_path, **kwargs):
        calls.append((seed_path, output_path))
        pd.DataFrame([{"Artist Name": "Atlas Bloom", "Source Directory": "Bandcamp"}]).to_csv(output_path, index=False)

    monkeypatch.setattr(cde, "run_cross_directory_enrichment", fake_run_cross_directory_enrichment)

    pipeline_runner.run_master_enrichment(
        seed_csv.as_posix(),
        output_csv.as_posix(),
        logger=None,
        enable_live_search=False,
        bandcamp_csv_path="",
    )

    assert len(calls) == 1


def test_enrichment_yield_tracker_counts_only_empty_to_non_empty_once_per_source_row():
    tracker = cde.EnrichmentYieldTracker()

    assert tracker.record_transition(
        7,
        {"Email": "", "Email_All": ""},
        {"Email": "web@example.com", "Email_All": "web@example.com"},
        "website_enrich",
    )
    assert not tracker.record_transition(
        7,
        {"Email": "", "Email_All": ""},
        {"Email": "web@example.com", "Email_All": "web@example.com"},
        "website_enrich",
    )
    assert not tracker.record_transition(
        7,
        {"Email": "seed@example.com", "Email_All": "seed@example.com"},
        {"Email": "seed@example.com", "Email_All": "seed@example.com;other@example.com"},
        "website_enrich",
    )
    assert tracker.record_transition(
        7,
        {"Email": "", "Email_All": ""},
        {"Email": "fb@example.com", "Email_All": "fb@example.com"},
        "facebook_enrich",
    )

    assert tracker.counts == {"website": 1, "facebook": 1}


def test_apply_payload_records_payload_source_yield_once(tmp_path):
    worker, _ = _build_worker(tmp_path)
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Atlas Bloom",
                "Email": "",
                "Email_All": "",
                "Social Link": "",
                "External Links": "",
                "Source Directory": "",
                "Source URL": "",
            }
        ]
    )
    payload = cde.EnrichmentPayload(
        socials=set(),
        websites=set(),
        emails={"hello@example.com"},
        link_hubs=set(),
        source_dir="soundcloud",
        source_url="https://soundcloud.com/atlasbloom",
        source_detail="SoundCloud (live search)",
    )

    worker._apply_payload(df, 0, payload)
    worker._apply_payload(df, 0, payload)

    assert worker._yield_tracker.counts == {"soundcloud": 1}


def test_run_master_enrichment_logs_enrichment_yield_once_with_festival_second_pass(tmp_path, monkeypatch):
    seed_csv = tmp_path / "master_raw.csv"
    output_csv = tmp_path / "master_enriched.csv"
    pd.DataFrame([{"Artist Name": "Atlas Bloom", "Seed Priority": "festival"}]).to_csv(seed_csv, index=False)

    calls = []
    logs = []

    def fake_run_cross_directory_enrichment(seed_path, output_path, **kwargs):
        calls.append((seed_path, output_path))
        tracker = kwargs.get("yield_tracker")
        assert tracker is not None
        if len(calls) == 1:
            pd.DataFrame([{"Artist Name": "Atlas Bloom", "Source Directory": "Bandcamp"}]).to_csv(output_path, index=False)
            pd.DataFrame(
                [
                    {
                        "Artist Name": "Solar Harbor",
                        "Expansion Parent": "Atlas Bloom",
                        "Expansion Origin": "bandcamp",
                        "Discovery Tier": "festival_expansion",
                    }
                ]
            ).to_csv(cde._festival_expansion_raw_path(output_path), index=False)
            tracker.record_transition(
                0,
                {"Email": "", "Email_All": ""},
                {"Email": "web@example.com", "Email_All": "web@example.com"},
                "website_enrich",
            )
        else:
            pd.DataFrame(
                [
                    {
                        "Artist Name": "Solar Harbor",
                        "Source Directory": "Bandcamp",
                        "Expansion Parent": "Atlas Bloom",
                        "Expansion Origin": "bandcamp",
                        "Discovery Tier": "festival_expansion",
                    }
                ]
            ).to_csv(output_path, index=False)
            tracker.record_transition(
                1,
                {"Email": "", "Email_All": ""},
                {"Email": "fb@example.com", "Email_All": "fb@example.com"},
                "facebook_enrich",
            )
            tracker.record_transition(
                1,
                {"Email": "", "Email_All": ""},
                {"Email": "fb@example.com", "Email_All": "fb@example.com"},
                "facebook_enrich",
            )

    monkeypatch.setattr(cde, "run_cross_directory_enrichment", fake_run_cross_directory_enrichment)

    pipeline_runner.run_master_enrichment(
        seed_csv.as_posix(),
        output_csv.as_posix(),
        logger=logs.append,
        enable_live_search=False,
        bandcamp_csv_path="",
    )

    summary_text = "\n".join(logs)
    assert len(calls) == 2
    assert summary_text.count("[Enrichment Yield]") == 1
    assert "[Enrichment Yield]" in summary_text
    assert "website=1" in summary_text
    assert "facebook=1" in summary_text
