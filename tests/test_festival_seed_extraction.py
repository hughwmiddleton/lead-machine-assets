import logging
from pathlib import Path

import pandas as pd

import festival_scraper
import night_mode_runner
import pipeline_runner


def test_scrape_festivals_parses_and_dedupes(monkeypatch) -> None:
    html = """
    <html>
      <body>
        <div class="artist-card"><h3>  Alpha  </h3></div>
        <div class="artist-card"><h3>Beta</h3></div>
        <div class="artist-card"><h3>Alpha</h3></div>
        <div class="artist-card"><h3>   </h3></div>
      </body>
    </html>
    """

    monkeypatch.setattr(
        festival_scraper,
        "fetch_html",
        lambda url, **kwargs: {
            "status": 200,
            "final_url": url,
            "html": html,
            "mode_used": "requests",
            "reason": "ok",
            "elapsed_ms": 1,
            "domain": "example.com",
        },
    )

    rows = festival_scraper.scrape_festivals(
        params={"festival_keys": ["bigsound"]},
        logger=None,
    )

    assert [row["Artist Name"] for row in rows] == ["Alpha", "Beta"]
    assert all(row["Source Directory"] == "festival_bigsound" for row in rows)
    assert all(row["Source URL"] == festival_scraper.FESTIVAL_CONFIG["bigsound"]["url"] for row in rows)
    assert all("Date Added" in row and row["Date Added"] for row in rows)
    assert all(row["Email"] == "" for row in rows)
    assert all(row["Festival Sources"] == "bigsound" for row in rows)
    assert all(row["Festival Count"] == "1" for row in rows)
    assert all(row["Seed Priority"] == "festival" for row in rows)


def test_scrape_festivals_aggregates_across_sources(monkeypatch) -> None:
    html_by_url = {
        festival_scraper.FESTIVAL_CONFIG["bigsound"]["url"]: """
        <html><body>
          <div class="artist-card"><h3>Lunar Echo</h3></div>
          <div class="artist-card"><h3>Atlas Bloom</h3></div>
        </body></html>
        """,
        festival_scraper.FESTIVAL_CONFIG["sxsw"]["url"]: """
        <html><body>
          <div class="lineup-item"><h3>Lunar Echo</h3></div>
        </body></html>
        """,
    }

    def fake_fetch_html(url, **kwargs):
        return {
            "status": 200,
            "final_url": url,
            "html": html_by_url.get(url, ""),
            "mode_used": "requests",
            "reason": "ok",
            "elapsed_ms": 1,
            "domain": "example.com",
        }

    monkeypatch.setattr(festival_scraper, "fetch_html", fake_fetch_html)

    rows = festival_scraper.scrape_festivals(
        params={"festival_keys": ["bigsound", "sxsw"]},
        logger=None,
    )

    assert [row["Artist Name"] for row in rows] == ["Lunar Echo", "Atlas Bloom"]
    lunar_echo = rows[0]
    assert lunar_echo["Festival Sources"] == "bigsound;sxsw"
    assert lunar_echo["Festival Count"] == "2"
    assert lunar_echo["Seed Priority"] == "festival_high"
    assert lunar_echo["Source Directory"] == "festival_bigsound"
    assert lunar_echo["Source URL"] == festival_scraper.FESTIVAL_CONFIG["bigsound"]["url"]


def test_run_directory_job_festival_writes_raw_csv(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: object())
    monkeypatch.setattr(
        festival_scraper,
        "scrape_festivals",
        lambda max_artists=None, params=None, logger=None: [
            {
                "Artist Name": "Festival Artist",
                "Festival Sources": "sxsw",
                "Festival Count": 1,
                "Seed Priority": "festival",
                "Source Directory": "festival_sxsw",
                "Source URL": "https://www.sxsw.com/music/festival-lineup",
                "Email": "",
            }
        ],
    )

    raw_path = tmp_path / "raw.csv"
    result_path = pipeline_runner.run_directory_job(
        {
            "directory": "festival",
            "job_id": "job_festival_1",
            "festival_keys": ["sxsw"],
        },
        raw_path.as_posix(),
        logger=None,
    )

    assert Path(result_path).exists()
    assert raw_path.exists()
    assert not (tmp_path / "raw.tmp.csv").exists()

    df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
    assert list(df["Artist Name"]) == ["Festival Artist"]
    assert df.at[0, "Festival Sources"] == "sxsw"
    assert df.at[0, "Festival Count"] == "1"
    assert df.at[0, "Seed Priority"] == "festival"
    assert df.at[0, "Source Directory"] == "festival_sxsw"
    assert df.at[0, "Source URL"] == "https://www.sxsw.com/music/festival-lineup"


def test_merge_raw_master_preserves_festival_metadata(tmp_path: Path) -> None:
    run_dir = tmp_path
    festival_csv = run_dir / "job_festival_raw.csv"
    spotify_csv = run_dir / "job_spotify_raw.csv"

    pd.DataFrame(
        [
            {
                "Artist Name": "Lunar Echo",
                "Email": "",
                "Festival Sources": "bigsound;sxsw",
                "Festival Count": "2",
                "Seed Priority": "festival_high",
                "Source Directory": "festival_bigsound",
                "Source URL": "https://www.bigsound.org.au/artists",
            }
        ]
    ).to_csv(festival_csv, index=False)
    pd.DataFrame(
        [
            {
                "Artist Name": "Playlist Artist",
                "Email": "",
                "Source Directory": "spotify",
                "Source URL": "https://open.spotify.com/artist/abc",
            }
        ]
    ).to_csv(spotify_csv, index=False)

    logger = logging.getLogger("test_festival_merge")
    master_path = night_mode_runner._merge_raw_master(
        run_dir.as_posix(),
        [
            {"job_id": "job_festival", "raw_csv": festival_csv.as_posix()},
            {"job_id": "job_spotify", "raw_csv": spotify_csv.as_posix()},
        ],
        logger,
    )

    assert master_path is not None
    merged = pd.read_csv(master_path, dtype=str, keep_default_na=False)
    assert "Festival Sources" in merged.columns
    assert "Festival Count" in merged.columns
    assert "Seed Priority" in merged.columns

    festival_row = merged.loc[merged["Artist Name"] == "Lunar Echo"].iloc[0]
    assert festival_row["Festival Sources"] == "bigsound;sxsw"
    assert festival_row["Festival Count"] == "2"
    assert festival_row["Seed Priority"] == "festival_high"
