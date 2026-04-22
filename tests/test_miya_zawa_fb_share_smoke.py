from __future__ import annotations

from pathlib import Path

import pandas as pd

import pipeline_runner
from scripts.run_miya_zawa_share_smoke import DEFAULT_FIXTURE, run_smoke


def _fixture_row() -> dict[str, str]:
    df = pd.read_csv(DEFAULT_FIXTURE, dtype=str, keep_default_na=False).fillna("")
    assert len(df.index) == 1
    return df.iloc[0].to_dict()


def test_miya_zawa_fixture_preserves_raw_share_shape() -> None:
    row = _fixture_row()

    assert row["Artist Name"] == "miya-zawa"
    assert row["Location"] == "Canberra, ACT"
    assert row["Song Title"] == "Don't Talk"
    assert "spill tab, SZA, Benee" == row["Sounds Like"]
    assert "https://www.facebook.com/share/15ZPp4vohe/?mibextid=wwXIfr" in row["Social Link"]
    assert row["Facebook_URL"] == ""
    assert row["Facebook URL"] == ""
    assert row["facebook_url"] == ""
    assert row["Source Directory"] == "Triple J Unearthed"


def test_miya_zawa_resolved_smoke_updates_canonical_field_before_intake(tmp_path: Path) -> None:
    canonical_url = "https://www.facebook.com/itsmiyazawa"

    artifacts = run_smoke(
        fixture_path=DEFAULT_FIXTURE,
        output_root=tmp_path,
        mode="resolved",
        resolved_url=canonical_url,
    )

    assert artifacts.summary["input_rows"] == 1
    assert artifacts.summary["canonical_facebook_url"] == canonical_url
    assert artifacts.summary["facebook_url_alias"] == canonical_url
    assert artifacts.summary["facebook_url_title_alias"] == canonical_url
    assert artifacts.summary["fb_url_present"] is True
    assert artifacts.summary["fb_entrypoint_present"] is True
    assert artifacts.summary["explicit_intake_outcome"] == "attempt"
    assert artifacts.summary["fb_scrape_started"] is True
    assert artifacts.summary["helper_calls"] == 1
    assert artifacts.summary["helper_urls"] == [canonical_url]
    assert artifacts.summary["pass_a_attempted"] == 1
    assert artifacts.summary["night_fb_discovery_skipped"] is False
    assert artifacts.summary["discovery_logs_present"] is False
    assert artifacts.row["Social Link"] == _fixture_row()["Social Link"]

    detected_idx = next(i for i, line in enumerate(artifacts.log_lines) if "[FB Share Canonicalize]" in line and "detected=1" in line)
    resolved_idx = next(i for i, line in enumerate(artifacts.log_lines) if "[FB Share Canonicalize]" in line and "outcome='resolved'" in line)
    gate_idx = next(i for i, line in enumerate(artifacts.log_lines) if "[Night FB][Row Gate]" in line)
    intake_idx = next(i for i, line in enumerate(artifacts.log_lines) if "[Night FB][Explicit Intake]" in line and 'outcome="attempt"' in line)

    assert detected_idx < resolved_idx < gate_idx < intake_idx
    assert any("artist='miya-zawa'" in line and "fb_url_present=True" in line and "fb_entrypoint_present=True" in line for line in artifacts.log_lines)
    assert any('[Night FB][Explicit Intake]' in line and f'urls="{canonical_url}"' in line for line in artifacts.log_lines)
    assert any(f"[FB Email] Visiting {canonical_url}" in line for line in artifacts.log_lines)


def test_miya_zawa_unresolved_smoke_remains_truthful_without_fake_url(tmp_path: Path) -> None:
    artifacts = run_smoke(
        fixture_path=DEFAULT_FIXTURE,
        output_root=tmp_path,
        mode="unresolved",
    )

    assert artifacts.summary["input_rows"] == 1
    assert artifacts.summary["canonical_facebook_url"] == ""
    assert artifacts.summary["facebook_url_alias"] == ""
    assert artifacts.summary["facebook_url_title_alias"] == ""
    assert artifacts.summary["fb_url_present"] is False
    assert artifacts.summary["fb_entrypoint_present"] is False
    assert artifacts.summary["explicit_intake_outcome"] == "no_explicit_url"
    assert artifacts.summary["fb_scrape_started"] is False
    assert artifacts.summary["helper_calls"] == 0
    assert artifacts.summary["helper_urls"] == []
    assert artifacts.summary["pass_a_attempted"] == 0
    assert artifacts.summary["night_fb_discovery_skipped"] is True
    assert artifacts.summary["discovery_logs_present"] is False
    assert artifacts.row["Social Link"] == _fixture_row()["Social Link"]
    assert any("[FB Share Canonicalize]" in line and "outcome='unresolved'" in line for line in artifacts.log_lines)
    assert any('[Night FB][Explicit Intake]' in line and 'outcome="no_explicit_url"' in line for line in artifacts.log_lines)
    assert not any("[FB Email] Visiting" in line for line in artifacts.log_lines)


def test_non_share_facebook_rows_remain_unaffected_by_share_resolver() -> None:
    df = pd.DataFrame(
        [
            {
                "Artist Name": "Direct FB Artist",
                "Social Link": "https://www.instagram.com/directfb, https://www.facebook.com/directfbartist",
                "Facebook_URL": "",
                "Facebook URL": "",
                "facebook_url": "",
            }
        ],
        dtype=str,
    ).fillna("")

    def _fail_share_resolver(raw: str) -> str:
        raise AssertionError(f"share resolver should not be called for non-share URLs: {raw}")

    pipeline_runner._promote_fb_urls_df(df, share_resolver=_fail_share_resolver)

    assert df.at[0, "Facebook_URL"] == "https://www.facebook.com/directfbartist"
    assert df.at[0, "Facebook URL"] == "https://www.facebook.com/directfbartist"
    assert df.at[0, "facebook_url"] == "https://www.facebook.com/directfbartist"
