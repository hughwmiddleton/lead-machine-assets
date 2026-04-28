from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import pipeline_runner
import pytest


def _read_index(path):
    with open(path, "r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _artist_url(slug: str) -> str:
    return f"https://www.abc.net.au/triplejunearthed/artist/{slug}"


def _write_cursor(path, index_path, last_position: int, last_url: str) -> None:
    path.write_text(
        json.dumps(
            {
                "index_file_path": str(index_path),
                "last_position": last_position,
                "last_url": last_url,
                "timestamp": "2026-04-28T00:00:00+00:00",
                "batch_size": last_position + 1,
            }
        ),
        encoding="utf-8",
    )


def _write_index_urls(module, index_path, urls: list[str]) -> None:
    rows = []
    for url in urls:
        normalized_url, slug = module.normalize_unearthed_artist_url(url)
        rows.append(
            {
                "artist_url": normalized_url,
                "artist_slug": slug,
                "first_seen_at": "2026-04-28T00:00:00+00:00",
                "last_seen_at": "2026-04-28T00:00:00+00:00",
                "source": "test",
            }
        )
    module._write_unearthed_artist_url_index(rows, index_path=str(index_path), require_existing=False)


def _run_index_scrape(
    module,
    monkeypatch,
    tmp_path,
    index_path,
    cursor_path,
    *,
    max_artists,
    resume_mode,
    manual_start=None,
):
    class Driver:
        page_source = ""

        def get(self, url):
            raise AssertionError(f"index mode should not open listing URL: {url}")

        def quit(self):
            return None

    scraped = []
    monkeypatch.setattr(module, "setup_driver", lambda: Driver())
    monkeypatch.setattr(module, "_unearthed_cursor_path", lambda: str(cursor_path))
    monkeypatch.setattr(module, "get_drum_status_from_source", lambda _html: "")
    monkeypatch.setattr(
        module,
        "scrape_artist_profile",
        lambda _driver, profile_url, fb_driver=None: (
            scraped.append(profile_url) or ("", "", "", "", profile_url.rsplit("/", 1)[-1], "", "", "", "")
        ),
    )
    monkeypatch.setattr(module, "save_to_csv", lambda *_args, **_kwargs: None)

    job_config = {
        "use_unearthed_url_index": True,
        "unearthed_resume_mode": resume_mode,
        "unearthed_url_index_path": str(index_path),
    }
    if manual_start is not None:
        job_config["unearthed_start_index_position"] = manual_start

    module.scrape_website(
        "https://www.abc.net.au/triplejunearthed",
        existing_csv=str(tmp_path / "raw.csv"),
        max_artists=max_artists,
        job_config=job_config,
    )
    return scraped


def test_unearthed_url_index_upsert_dedupes_and_preserves_first_seen(tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    index_path.write_text(
        "artist_url,artist_slug,first_seen_at,last_seen_at,source\n"
        "https://www.abc.net.au/triplejunearthed/artist/artist-one,artist-one,2026-01-01T00:00:00+00:00,2026-01-01T00:00:00+00:00,discovery\n",
        encoding="utf-8",
    )

    result = module.upsert_unearthed_artist_url_index(
        [
            "https://www.abc.net.au/triplejunearthed/artist/Artist-One/?utm=ignored",
            "artist-two",
            "https://www.abc.net.au/triplejunearthed/artist/artist-two",
        ],
        index_path=str(index_path),
    )

    rows = _read_index(index_path)
    assert result["new_urls"] == 1
    assert result["updated_existing"] == 2
    assert result["total"] == 2
    assert [row["artist_slug"] for row in rows] == ["artist-one", "artist-two"]
    assert rows[0]["artist_url"] == "https://www.abc.net.au/triplejunearthed/artist/artist-one"
    assert rows[0]["first_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert rows[0]["last_seen_at"] != "2026-01-01T00:00:00+00:00"


def test_unearthed_discovery_persists_urls_incrementally(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    module._write_empty_unearthed_artist_url_index(str(index_path))

    class Driver:
        page_source = (
            '<html><body>'
            '<a href="/triplejunearthed/artist/Artist-One?x=1">One</a>'
            '<a href="/triplejunearthed/artist/artist-two/">Two</a>'
            "</body></html>"
        )

        def get(self, _url):
            return None

        def quit(self):
            return None

    class Wait:
        def __init__(self, _driver, _timeout):
            pass

        def until(self, condition):
            if condition[0] == "presence":
                return True
            raise RuntimeError("no more pages")

    scraped = []
    monkeypatch.setattr(module, "setup_driver", lambda: Driver())
    monkeypatch.setattr(module, "WebDriverWait", Wait)
    monkeypatch.setattr(
        module,
        "EC",
        SimpleNamespace(
            presence_of_element_located=lambda locator: ("presence", locator),
            element_to_be_clickable=lambda locator: ("clickable", locator),
        ),
    )
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.random, "uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(module, "get_drum_status_from_source", lambda _html: "")
    monkeypatch.setattr(
        module,
        "scrape_artist_profile",
        lambda _driver, profile_url, fb_driver=None: (
            scraped.append(profile_url) or ("", "", "", "", profile_url.rsplit("/", 1)[-1], "", "", "", "")
        ),
    )
    monkeypatch.setattr(module, "save_to_csv", lambda *_args, **_kwargs: None)

    module.scrape_website(
        "https://www.abc.net.au/triplejunearthed",
        existing_csv=str(tmp_path / "raw.csv"),
        max_artists=2,
        job_config={"unearthed_url_index_path": str(index_path)},
    )

    rows = _read_index(index_path)
    assert [row["artist_url"] for row in rows] == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-one",
        "https://www.abc.net.au/triplejunearthed/artist/artist-two",
    ]
    assert scraped == [row["artist_url"] for row in rows]


def test_unearthed_index_mode_first_run_starts_at_zero_and_persists_cursor(monkeypatch, tmp_path, capsys):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    module.upsert_unearthed_artist_url_index(
        [_artist_url("artist-b"), _artist_url("artist-a"), _artist_url("artist-c")],
        index_path=str(index_path),
    )

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=2,
        resume_mode="cursor",
    )
    captured = capsys.readouterr()
    payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    assert scraped == [_artist_url("artist-b"), _artist_url("artist-a")]
    assert "[UE Index Cursor] no valid cursor found" in captured.out
    assert "reason=missing_file" in captured.out
    assert "[UE Index Cursor] mode=cursor index_rows=3 start=0 end=1 count=2" in captured.out
    assert payload["index_file_path"] == str(index_path)
    assert payload["last_position"] == 1
    assert payload["last_url"] == _artist_url("artist-a")


def test_unearthed_index_continue_run_advances_from_persisted_position(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(5)]
    module.upsert_unearthed_artist_url_index(urls, index_path=str(index_path))
    _write_cursor(cursor_path, index_path, 1, urls[1])

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=2,
        resume_mode="cursor",
    )
    payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    assert scraped == urls[2:4]
    assert payload["last_position"] == 3
    assert payload["last_url"] == urls[3]


def test_unearthed_index_blank_manual_start_uses_cursor(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(6)]
    _write_index_urls(module, index_path, urls)
    _write_cursor(cursor_path, index_path, 1, urls[1])

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=2,
        resume_mode="cursor",
        manual_start="",
    )
    payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    assert scraped == urls[2:4]
    assert payload["last_position"] == 3
    assert payload["last_url"] == urls[3]


def test_unearthed_index_manual_start_1500_selects_expected_slice_and_persists(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(3100)]
    _write_index_urls(module, index_path, urls)
    _write_cursor(cursor_path, index_path, 1499, urls[1499])

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=1500,
        resume_mode="cursor",
        manual_start=1500,
    )
    payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    assert scraped == urls[1500:3000]
    assert payload["last_position"] == 2999
    assert payload["last_url"] == urls[2999]
    assert set(urls[0:1500]).isdisjoint(set(scraped))


def test_unearthed_index_startup_logging_markers(monkeypatch, tmp_path, capsys):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(20)]
    _write_index_urls(module, index_path, urls)

    _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=10,
        resume_mode="cursor",
        manual_start=5,
    )
    captured = capsys.readouterr()

    assert "[UE Startup] job_entry" in captured.out
    assert "[UE Startup] index_loaded" in captured.out
    assert "[UE Startup] index_slice_done" in captured.out
    assert "[UE Startup] first_profile_prepare" in captured.out


def test_unearthed_index_manual_start_overrides_cursor(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(10)]
    _write_index_urls(module, index_path, urls)
    _write_cursor(cursor_path, index_path, 6, urls[6])

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=3,
        resume_mode="cursor",
        manual_start=2,
    )
    payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    assert scraped == urls[2:5]
    assert payload["last_position"] == 4
    assert payload["last_url"] == urls[4]


def test_unearthed_index_invalid_manual_start_does_not_corrupt_cursor(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(6)]
    _write_index_urls(module, index_path, urls)
    _write_cursor(cursor_path, index_path, 2, urls[2])
    original_payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=2,
        resume_mode="cursor",
        manual_start="not-an-int",
    )

    assert scraped == []
    assert json.loads(cursor_path.read_text(encoding="utf-8")) == original_payload


def test_unearthed_index_negative_manual_start_does_not_corrupt_cursor(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(6)]
    _write_index_urls(module, index_path, urls)
    _write_cursor(cursor_path, index_path, 2, urls[2])
    original_payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=2,
        resume_mode="cursor",
        manual_start=-1,
    )

    assert scraped == []
    assert json.loads(cursor_path.read_text(encoding="utf-8")) == original_payload


def test_unearthed_index_manual_start_beyond_index_clamps_without_cursor_write(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(6)]
    _write_index_urls(module, index_path, urls)
    _write_cursor(cursor_path, index_path, 2, urls[2])
    original_payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=2,
        resume_mode="cursor",
        manual_start=100,
    )

    assert scraped == []
    assert json.loads(cursor_path.read_text(encoding="utf-8")) == original_payload


def test_unearthed_index_cursor_is_bound_to_index_path(monkeypatch, tmp_path, capsys):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    other_index_path = tmp_path / "other_unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(4)]
    module.upsert_unearthed_artist_url_index(urls, index_path=str(index_path))
    _write_cursor(cursor_path, other_index_path, 1, urls[1])

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=2,
        resume_mode="cursor",
    )
    captured = capsys.readouterr()

    assert scraped == urls[0:2]
    assert "[UE Index Cursor] ignored cursor reason=index_path_mismatch" in captured.out


def test_unearthed_index_start_fresh_ignores_existing_cursor(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(4)]
    module.upsert_unearthed_artist_url_index(urls, index_path=str(index_path))
    _write_cursor(cursor_path, index_path, 2, urls[2])
    monkeypatch.setattr(
        module,
        "_load_unearthed_index_cursor",
        lambda _index_path: (_ for _ in ()).throw(AssertionError("fresh mode must not read cursor")),
    )

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=2,
        resume_mode="fresh",
    )
    payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    assert scraped == urls[0:2]
    assert payload["last_position"] == 1
    assert payload["last_url"] == urls[1]


def test_unearthed_index_cursor_end_clamps_safely(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    urls = [_artist_url(f"artist-{idx}") for idx in range(10)]
    module.upsert_unearthed_artist_url_index(urls, index_path=str(index_path))
    _write_cursor(cursor_path, index_path, 7, urls[7])

    scraped = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=5,
        resume_mode="cursor",
    )
    payload = json.loads(cursor_path.read_text(encoding="utf-8"))

    assert scraped == urls[8:10]
    assert payload["last_position"] == 9
    assert payload["last_url"] == urls[9]


def test_unearthed_index_no_replay_and_no_overlap_between_runs(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    cursor_path = tmp_path / "unearthed_cursor.json"
    slugs = ["chez", "blake-rose", "jem-cassar-daley", "shewita", "selve", "next-a", "next-b"]
    urls = [_artist_url(slug) for slug in slugs]
    module.upsert_unearthed_artist_url_index(urls, index_path=str(index_path))

    batch_1 = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=5,
        resume_mode="cursor",
    )
    batch_2 = _run_index_scrape(
        module,
        monkeypatch,
        tmp_path,
        index_path,
        cursor_path,
        max_artists=2,
        resume_mode="cursor",
    )

    assert batch_1 == urls[0:5]
    assert batch_2 == urls[5:7]
    assert _artist_url("chez") not in batch_2
    assert _artist_url("blake-rose") not in batch_2
    assert set(batch_1).isdisjoint(set(batch_2))


def test_unearthed_index_backfill_is_explicit_and_does_not_modify_source(tmp_path):
    module = pipeline_runner._load_legacy_module()
    source_path = tmp_path / "master_raw.csv"
    source_text = "Artist Name,Source URL\nArtist Three,\nIgnored,https://example.com/not-unearthed\n"
    source_path.write_text(source_text, encoding="utf-8")
    index_path = tmp_path / "unearthed_artist_url_index.csv"

    module.backfill_unearthed_artist_url_index([str(source_path)], index_path=str(index_path))

    assert source_path.read_text(encoding="utf-8") == source_text
    rows = _read_index(index_path)
    assert [row["artist_url"] for row in rows] == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-three"
    ]


def test_unearthed_explicit_missing_index_path_fails_loudly(tmp_path):
    module = pipeline_runner._load_legacy_module()
    missing_path = tmp_path / "missing.csv"

    with pytest.raises(FileNotFoundError):
        module.scrape_website(
            "https://www.abc.net.au/triplejunearthed",
            existing_csv=str(tmp_path / "raw.csv"),
            max_artists=1,
            job_config={"unearthed_url_index_path": str(missing_path)},
        )
