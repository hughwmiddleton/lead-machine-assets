from pathlib import Path

import pandas as pd

import festival_scraper
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


def test_run_directory_job_festival_writes_raw_csv(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pipeline_runner, "_load_legacy_module", lambda: object())
    monkeypatch.setattr(
        festival_scraper,
        "scrape_festivals",
        lambda max_artists=None, params=None, logger=None: [
            {
                "Artist Name": "Festival Artist",
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
    assert df.at[0, "Source Directory"] == "festival_sxsw"
    assert df.at[0, "Source URL"] == "https://www.sxsw.com/music/festival-lineup"
