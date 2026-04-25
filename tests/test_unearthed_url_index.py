from __future__ import annotations

import csv
from types import SimpleNamespace

import pipeline_runner


def _read_index(path):
    with open(path, "r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


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


def test_unearthed_index_mode_uses_stable_index_order_and_skips_cursor(monkeypatch, tmp_path):
    module = pipeline_runner._load_legacy_module()
    index_path = tmp_path / "unearthed_artist_url_index.csv"
    module.upsert_unearthed_artist_url_index(
        [
            "https://www.abc.net.au/triplejunearthed/artist/artist-b",
            "https://www.abc.net.au/triplejunearthed/artist/artist-a",
        ],
        index_path=str(index_path),
    )

    class Driver:
        page_source = ""

        def get(self, url):
            raise AssertionError(f"index mode should not open listing URL: {url}")

        def quit(self):
            return None

    scraped = []
    monkeypatch.setattr(module, "setup_driver", lambda: Driver())
    monkeypatch.setattr(
        module,
        "_load_unearthed_persistent_cursor",
        lambda: (_ for _ in ()).throw(AssertionError("cursor should not be loaded")),
    )
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
        max_artists=10,
        job_config={
            "use_unearthed_url_index": True,
            "unearthed_resume_mode": "cursor",
            "unearthed_url_index_path": str(index_path),
        },
    )

    assert scraped == [
        "https://www.abc.net.au/triplejunearthed/artist/artist-b",
        "https://www.abc.net.au/triplejunearthed/artist/artist-a",
    ]


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
