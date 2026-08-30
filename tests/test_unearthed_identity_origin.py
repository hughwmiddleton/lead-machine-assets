import csv
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import final_checker
import pipeline_runner
from cross_directory_enricher import CrossDirectoryEnricherWorker, EnrichmentPayload
from lead_vault.merge import merge_csv_into_master
from lead_vault.schema import get_canonical_master_schema


PROFILE_ROOT = "https://www.abc.net.au/triplejunearthed/artist"


class _StaticDriver:
    def __init__(self, html: str = "") -> None:
        self.page_source = html

    def get(self, _url: str) -> None:
        return None

    def quit(self) -> None:
        return None


class _ImmediateWait:
    def __init__(self, _driver, _timeout) -> None:
        pass

    def until(self, _condition):
        return True


def _disable_profile_side_effects(module, monkeypatch) -> None:
    monkeypatch.setattr(module, "WebDriverWait", _ImmediateWait)
    monkeypatch.setattr(
        module,
        "EC",
        SimpleNamespace(presence_of_element_located=lambda locator: locator),
    )
    monkeypatch.setattr(module, "SCRAPE_FB_EMAILS_ON_UNEARTHED_PAGE1", False)


@pytest.mark.parametrize(
    ("slug", "heading", "expected"),
    [
        ("mariae-cassandra", "Artist:Mariae Cassandra", "Mariae Cassandra"),
        ("xaad", "Artist:XääD", "XääD"),
    ],
)
def test_unearthed_profile_prefers_source_display_identity(monkeypatch, slug, heading, expected) -> None:
    module = pipeline_runner._load_legacy_module()
    _disable_profile_side_effects(module, monkeypatch)
    driver = _StaticDriver(f"<html><body><h1>{heading}</h1></body></html>")

    result = module.scrape_artist_profile(driver, f"{PROFILE_ROOT}/{slug}")

    assert result[4] == expected


def test_unearthed_profile_keeps_safe_slug_fallback_without_display_identity(monkeypatch) -> None:
    module = pipeline_runner._load_legacy_module()
    _disable_profile_side_effects(module, monkeypatch)

    result = module.scrape_artist_profile(_StaticDriver("<html><body></body></html>"), f"{PROFILE_ROOT}/fallback-act")

    assert result[4] == "fallback-act"


def test_unearthed_seed_row_initializes_display_identity_and_canonical_origin(monkeypatch, tmp_path) -> None:
    module = pipeline_runner._load_legacy_module()
    profile_url = f"{PROFILE_ROOT}/display-name-slug"
    index_path = tmp_path / "index.csv"
    output_path = tmp_path / "raw.csv"
    module._write_empty_unearthed_artist_url_index(str(index_path))
    module.upsert_unearthed_artist_url_index([profile_url], index_path=str(index_path))

    monkeypatch.setattr(module, "setup_driver", lambda: _StaticDriver())
    monkeypatch.setattr(module, "get_drum_status_from_source", lambda _html: "")
    monkeypatch.setattr(module, "update_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "init_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_unearthed_cursor_path", lambda: str(tmp_path / "cursor.json"))
    monkeypatch.setattr(
        module,
        "scrape_artist_profile",
        lambda _driver, _profile_url, fb_driver=None: (
            [],
            "Melbourne, VIC",
            "Track",
            "",
            "Official Display Name",
            "",
            "indie",
            "Indie",
            "",
        ),
    )
    _disable_profile_side_effects(module, monkeypatch)

    module.scrape_website(
        module.UNEARTHED_DEFAULT_URL,
        existing_csv=str(output_path),
        max_artists=1,
        job_config={
            "use_unearthed_url_index": True,
            "unearthed_resume_mode": "fresh",
            "unearthed_start_index_position": 0,
            "unearthed_url_index_path": str(index_path),
        },
    )

    row = pd.read_csv(output_path, dtype=str, keep_default_na=False).iloc[0]
    assert row["Artist Name"] == "Official Display Name"
    assert row["Source URL"] == profile_url
    assert row["Source_URL"] == profile_url
    assert row["Lead_Source"] == "Unearthed"
    assert row["Source_Directory"] == "Unearthed"
    assert row["Source Directory"] == "Unearthed"


def test_unearthed_identity_and_origin_survive_enrichment_checker_and_export(tmp_path) -> None:
    profile_url = f"{PROFILE_ROOT}/xaad"
    origin_fields = ("Lead_Source", "Source_Directory", "Source Directory", "Source URL", "Source_URL")
    frame = pd.DataFrame(
        [
            {
                "Artist Name": "XääD",
                "Email": "xd@xaad.test",
                "Email_All": "xd@xaad.test",
                "Email_Source_URL": "https://xaad.test/contact",
                "Email_Source_Type": "website_enrich",
                "Email_Extract_Method": "mailto",
                "Social Link": "",
                "External Links": "",
                "SoundCloud Link": "",
                "Bandcamp_URL": "",
                "Lead_Source": "Unearthed",
                "Source_Directory": "Unearthed",
                "Source Directory": "Unearthed",
                "Source URL": profile_url,
                "Source_URL": profile_url,
                "final_status": "OK",
                "Needs_Review": "FALSE",
            }
        ]
    )
    before = frame.loc[0, ["Artist Name", *origin_fields]].to_dict()

    CrossDirectoryEnricherWorker._apply_payload(
        None,
        frame,
        0,
        EnrichmentPayload(
            socials={"https://www.instagram.com/xaad"},
            emails={"xd@xaad.test"},
            source_dir="website_enrich",
            source_url="https://xaad.test/contact",
        ),
    )
    assert frame.loc[0, ["Artist Name", *origin_fields]].to_dict() == before

    status_recomputed = pipeline_runner.recompute_final_status_post_enrichment(frame.copy(), logger=None)
    assert status_recomputed.loc[0, ["Artist Name", *origin_fields]].to_dict() == before

    enriched_path = tmp_path / "unearthed_enriched.csv"
    status_recomputed.to_csv(enriched_path, index=False)
    checked_path = final_checker.run_final_checker(str(enriched_path), stale_downgrade=False)
    checked = pd.read_csv(checked_path, dtype=str, keep_default_na=False)
    assert checked.loc[0, ["Artist Name", *origin_fields]].to_dict() == before

    export = pipeline_runner._build_final_export_frame(checked)
    assert export.at[0, "Artist Name"] == "XääD"
    assert export.at[0, "Source URL"] == profile_url
    policy_row = export.iloc[0].to_dict()
    policy_row["Email"] = policy_row["Primary Email"]
    policy_row["Email_All"] = policy_row["All Emails"]
    assert final_checker.filter_rows_for_export("studio_safe", [policy_row])


def test_lead_vault_matches_corrected_rows_by_canonical_unearthed_url(tmp_path) -> None:
    profile_url = f"{PROFILE_ROOT}/official-display-name"
    master_path = tmp_path / "master.csv"
    source_path = tmp_path / "incoming.csv"
    master_row = {field: "" for field in get_canonical_master_schema()}
    master_row.update(
        {
            "Artist": "Official Display Name",
            "Location": "Sydney, NSW",
            "Source_URL": profile_url,
            "Lead_Source": "Unearthed",
            "Source_Directory": "Unearthed",
        }
    )
    with master_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=get_canonical_master_schema())
        writer.writeheader()
        writer.writerow(master_row)
    with source_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Artist Name", "Location", "Source URL", "Lead_Source", "Source_Directory"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "Artist Name": "official-display-name",
                "Location": "Sydney, NSW",
                "Source URL": profile_url,
                "Lead_Source": "Unearthed",
                "Source_Directory": "Unearthed",
            }
        )

    result = merge_csv_into_master(source_path, master_path=master_path)

    with master_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert result["rows_added"] == 0
    assert len(rows) == 1
    assert rows[0]["Artist"] == "Official Display Name"
    assert rows[0]["Source_URL"] == profile_url


def test_shared_csv_writer_leaves_non_unearthed_origin_behavior_unchanged(tmp_path) -> None:
    module = pipeline_runner._load_legacy_module()
    output_path = tmp_path / "spotify.csv"
    module.save_to_csv(
        [
            (
                "Spotify Act",
                "Melbourne",
                "Track",
                "",
                [],
                "",
                "",
                "",
                "",
                "pop",
                "Pop",
                "",
                "",
                "",
                "Spotify",
                "Spotify",
                "Spotify",
            )
        ],
        str(output_path),
    )

    row = pd.read_csv(output_path, dtype=str, keep_default_na=False).iloc[0]
    assert row["Artist Name"] == "Spotify Act"
    assert row["Lead_Source"] == "Spotify"
    assert row["Source_Directory"] == "Spotify"
    assert row["Source Directory"] == "Spotify"
    assert row["Source URL"] == ""
    assert row["Source_URL"] == ""
