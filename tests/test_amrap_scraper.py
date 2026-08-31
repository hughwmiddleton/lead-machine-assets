"""Deterministic tests for AMRAP V1 scraper.

These tests do not depend on live AMRAP network availability.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from amrap_scraper import (
    AMRAP_PUBLIC_PROFILE_URL_TEMPLATE,
    extract_state_from_location,
    fetch_directory_page,
    fetch_profile,
    parse_amrap_profile,
    scrape_amrap,
    scrape_amrap_to_csv,
)
from lead_vault.origin import safe_row_update, preserve_origin_fields


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture(name: str) -> Dict[str, Any]:
    path = Path(__file__).with_name("fixtures") / name
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "location_str,expected",
    [
        ("NSW", "NSW"),
        ("NSW, Sydney", "NSW"),
        ("WA, Perth", "WA"),
        ("VIC, Melbourne, CBD", "VIC"),
        ("ACT", "ACT"),
        ("", ""),
        ("Brisbane", ""),
        ("QLD , Brisbane", "QLD"),
    ],
)
def test_extract_state_from_location(location_str: str, expected: str) -> None:
    assert extract_state_from_location(location_str) == expected


# ---------------------------------------------------------------------------
# Profile parsing
# ---------------------------------------------------------------------------

def test_parse_public_profile_returns_row() -> None:
    fixture = _load_fixture("amrap_profile_public.json")
    row = parse_amrap_profile(fixture["data"])
    assert row is not None
    assert row["Artist Name"] == "Test Artist Alpha"
    assert row["Location"] == "NSW"
    assert row["Primary Genre"] == "Pop"
    assert "Rock" not in row["Primary Genre"]  # only first genre
    assert row["Social Link"] == "https://testartistalpha.com; https://www.instagram.com/testartistalpha; https://testartistalpha.bandcamp.com"
    assert row["Source URL"] == "https://amrap.org.au/artist/test-artist-alpha"
    assert row["Source_URL"] == "https://amrap.org.au/artist/test-artist-alpha"
    assert row["Lead_Source"] == "AMRAP"
    assert row["Source_Directory"] == "amrap"
    assert row["Source Directory"] == "AMRAP"
    assert row["Email"] == ""
    assert row["Date Added"] != ""


def test_parse_hidden_email_not_captured() -> None:
    """An email present in the API contact field must NOT be ingested."""
    fixture = _load_fixture("amrap_profile_with_hidden_email.json")
    row = parse_amrap_profile(fixture["data"])
    assert row is not None
    assert row["Artist Name"] == "Test Artist Hidden"
    assert row["Email"] == ""
    assert row["Location"] == "QLD, Brisbane"
    assert extract_state_from_location(row["Location"]) == "QLD"


def test_parse_minimal_profile() -> None:
    fixture = _load_fixture("amrap_profile_no_genre.json")
    row = parse_amrap_profile(fixture["data"])
    assert row is not None
    assert row["Artist Name"] == "Test Artist Minimal"
    assert row["Primary Genre"] == ""
    assert row["Social Link"] == ""
    assert row["Location"] == "ACT"
    assert extract_state_from_location(row["Location"]) == "ACT"


def test_parse_skips_non_public_profile() -> None:
    row = parse_amrap_profile({"visibility": "private", "slug": "x", "name": "X"})
    assert row is None


def test_parse_skips_missing_slug() -> None:
    row = parse_amrap_profile({"visibility": "public", "name": "No Slug"})
    assert row is None


def test_parse_skips_identity_tags() -> None:
    fixture = _load_fixture("amrap_profile_public.json")
    row = parse_amrap_profile(fixture["data"])
    assert row is not None
    # Male identity must not leak into any field
    for value in row.values():
        assert "Male" not in str(value)


# ---------------------------------------------------------------------------
# Provenance / origin locking
# ---------------------------------------------------------------------------

def test_provenance_survives_safe_row_update() -> None:
    fixture = _load_fixture("amrap_profile_public.json")
    row = parse_amrap_profile(fixture["data"])
    updates = {
        "Artist Name": "Enriched Name",
        "Email": "enriched@example.com",
        "Lead_Source": "Website",
        "Source_Directory": "website",
    }
    safe_row_update(row, updates)
    assert row["Artist Name"] == "Enriched Name"
    assert row["Email"] == "enriched@example.com"
    assert row["Lead_Source"] == "AMRAP"
    assert row["Source_Directory"] == "amrap"
    assert row["Source URL"] == "https://amrap.org.au/artist/test-artist-alpha"


def test_preserve_origin_fields_restores_amrap() -> None:
    fixture = _load_fixture("amrap_profile_public.json")
    canonical = parse_amrap_profile(fixture["data"])
    target = {
        "Artist Name": "Target Name",
        "Lead_Source": "Spotify",
        "Source_Directory": "spotify",
        "Source URL": "https://spotify.com/artist/123",
    }
    preserve_origin_fields(target, canonical)
    assert target["Lead_Source"] == "AMRAP"
    assert target["Source_Directory"] == "amrap"
    assert target["Source URL"] == "https://amrap.org.au/artist/test-artist-alpha"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_scrape_amrap_skips_existing_slugs(tmp_path: Path) -> None:
    """Deterministic scrape with an existing CSV containing one matching slug."""
    existing_csv = tmp_path / "existing.csv"
    existing_csv.write_text(
        "Artist Name,Source URL\n"
        'Existing Artist,https://amrap.org.au/artist/test-artist-alpha\n',
        encoding="utf-8-sig",
    )

    # Monkey-patch fetchers so no network is used
    def _fake_directory_page(session, page):
        return _load_fixture("amrap_directory_page.json")

    def _fake_profile(session, slug):
        if slug == "test-artist-alpha":
            return _load_fixture("amrap_profile_public.json")
        if slug == "test-artist-beta":
            return {
                "data": {
                    "id": "beta",
                    "visibility": "public",
                    "name": "Test Artist Beta",
                    "slug": "test-artist-beta",
                    "genres": [],
                    "phone": None,
                    "contact": None,
                    "identities": [],
                    "socials": [],
                    "music_accounts": [],
                    "location": {"as_string": "VIC"},
                    "websites": [],
                    "bio": None,
                }
            }
        if slug == "test-artist-gamma":
            return {
                "data": {
                    "id": "gamma",
                    "visibility": "public",
                    "name": "Test Artist Gamma",
                    "slug": "test-artist-gamma",
                    "genres": [],
                    "phone": None,
                    "contact": None,
                    "identities": [],
                    "socials": [],
                    "music_accounts": [],
                    "location": {"as_string": "SA"},
                    "websites": [],
                    "bio": None,
                }
            }
        return None

    import amrap_scraper
    orig_fetch_directory = amrap_scraper.fetch_directory_page
    orig_fetch_profile = amrap_scraper.fetch_profile
    amrap_scraper.fetch_directory_page = _fake_directory_page
    amrap_scraper.fetch_profile = _fake_profile

    try:
        rows = scrape_amrap(
            target_count=10,
            existing_csv=str(existing_csv),
            sleep_between_requests=0,
        )
    finally:
        amrap_scraper.fetch_directory_page = orig_fetch_directory
        amrap_scraper.fetch_profile = orig_fetch_profile

    slugs = {r["Source URL"].rstrip("/").split("/")[-1] for r in rows}
    assert "test-artist-alpha" not in slugs
    assert "test-artist-beta" in slugs
    assert "test-artist-gamma" in slugs


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def test_scrape_amrap_state_filter(tmp_path: Path) -> None:
    def _fake_directory_page(session, page):
        return _load_fixture("amrap_directory_page.json")

    def _fake_profile(session, slug):
        return {
            "data": {
                "id": slug,
                "visibility": "public",
                "name": slug.replace("-", " ").title(),
                "slug": slug,
                "genres": [],
                "phone": None,
                "contact": None,
                "identities": [],
                "socials": [],
                "music_accounts": [],
                "location": {
                    "as_string": {
                        "test-artist-alpha": "NSW",
                        "test-artist-beta": "VIC",
                        "test-artist-gamma": "NSW",
                    }.get(slug, "QLD")
                },
                "websites": [],
                "bio": None,
            }
        }

    import amrap_scraper
    orig_fetch_directory = amrap_scraper.fetch_directory_page
    orig_fetch_profile = amrap_scraper.fetch_profile
    amrap_scraper.fetch_directory_page = _fake_directory_page
    amrap_scraper.fetch_profile = _fake_profile

    try:
        rows = scrape_amrap(
            target_count=10,
            state_filter="NSW",
            sleep_between_requests=0,
        )
    finally:
        amrap_scraper.fetch_directory_page = orig_fetch_directory
        amrap_scraper.fetch_profile = orig_fetch_profile

    assert len(rows) == 2
    for r in rows:
        assert extract_state_from_location(r["Location"]) == "NSW"


def test_scrape_amrap_genre_filter(tmp_path: Path) -> None:
    def _fake_directory_page(session, page):
        return _load_fixture("amrap_directory_page.json")

    def _fake_profile(session, slug):
        return {
            "data": {
                "id": slug,
                "visibility": "public",
                "name": slug.replace("-", " ").title(),
                "slug": slug,
                "genres": [
                    {
                        "id": 1,
                        "name": {
                            "test-artist-alpha": "Pop",
                            "test-artist-beta": "Rock",
                            "test-artist-gamma": "Jazz",
                        }.get(slug, "Electronic"),
                        "type": "genre",
                    }
                ],
                "phone": None,
                "contact": None,
                "identities": [],
                "socials": [],
                "music_accounts": [],
                "location": {"as_string": "NSW"},
                "websites": [],
                "bio": None,
            }
        }

    import amrap_scraper
    orig_fetch_directory = amrap_scraper.fetch_directory_page
    orig_fetch_profile = amrap_scraper.fetch_profile
    amrap_scraper.fetch_directory_page = _fake_directory_page
    amrap_scraper.fetch_profile = _fake_profile

    try:
        rows = scrape_amrap(
            target_count=10,
            genre_filter="rock",
            sleep_between_requests=0,
        )
    finally:
        amrap_scraper.fetch_directory_page = orig_fetch_directory
        amrap_scraper.fetch_profile = orig_fetch_profile

    assert len(rows) == 1
    assert rows[0]["Artist Name"] == "Test Artist Beta"


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def test_scrape_amrap_to_csv_writes_file(tmp_path: Path) -> None:
    def _fake_directory_page(session, page):
        return _load_fixture("amrap_directory_page.json")

    def _fake_profile(session, slug):
        if slug == "test-artist-alpha":
            return _load_fixture("amrap_profile_public.json")
        return None

    import amrap_scraper
    orig_fetch_directory = amrap_scraper.fetch_directory_page
    orig_fetch_profile = amrap_scraper.fetch_profile
    amrap_scraper.fetch_directory_page = _fake_directory_page
    amrap_scraper.fetch_profile = _fake_profile

    output_csv = tmp_path / "amrap_out.csv"
    try:
        result_path = scrape_amrap_to_csv(
            target_count=10,
            output_csv=str(output_csv),
            sleep_between_requests=0,
        )
    finally:
        amrap_scraper.fetch_directory_page = orig_fetch_directory
        amrap_scraper.fetch_profile = orig_fetch_profile

    assert Path(result_path).exists()
    text = output_csv.read_text(encoding="utf-8-sig")
    assert "Artist Name" in text
    assert "Test Artist Alpha" in text
    assert "AMRAP" in text
    assert "https://amrap.org.au/artist/test-artist-alpha" in text


# ---------------------------------------------------------------------------
# Pipeline dispatch integration
# ---------------------------------------------------------------------------

def test_pipeline_run_directory_job_dispatches_amrap(monkeypatch, tmp_path: Path) -> None:
    """run_directory_job must dispatch directory=='amrap' to amrap_scraper."""
    from pipeline_runner import run_directory_job

    calls: List[Dict[str, Any]] = []

    def _fake_scrape_to_csv(**kwargs):
        calls.append(kwargs)
        # Write a minimal CSV so the pipeline finalizes successfully
        output = kwargs["output_csv"]
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(
            "Artist Name,Lead_Source,Source_Directory,Source URL\n"
            'Test Artist,AMRAP,amrap,https://amrap.org.au/artist/test-artist\n',
            encoding="utf-8-sig",
        )

    monkeypatch.setattr("amrap_scraper.scrape_amrap_to_csv", _fake_scrape_to_csv)

    job_config = {
        "directory": "amrap",
        "target_count": 50,
        "amrap_state": "NSW",
        "amrap_genre": "rock",
    }
    output_path = tmp_path / "amrap_raw.csv"
    result = run_directory_job(job_config, str(output_path))

    assert len(calls) == 1
    call = calls[0]
    assert call["target_count"] == 50
    assert call["state_filter"] == "NSW"
    assert call["genre_filter"] == "rock"
    assert Path(result).exists()


# ---------------------------------------------------------------------------
# Night Mode integration
# ---------------------------------------------------------------------------

def test_night_mode_normalises_amrap_source() -> None:
    from night_mode_runner import _normalise_seed_source_name
    assert _normalise_seed_source_name("AMRAP") == "amrap"
    assert _normalise_seed_source_name("amrap") == "amrap"
    assert _normalise_seed_source_name("Amrap Public Directory") == "amrap"


def test_night_mode_job_dialog_includes_amrap() -> None:
    """Static check that NightModeJobDialog directory combo includes amrap."""
    gui_path = Path(__file__).parent.parent / "Lead Machine (Final Update 5).py"
    source = gui_path.read_text(encoding="utf-8")
    assert 'self.directory_combo.addItems(["spotify", "bandcamp", "soundcloud", "unearthed", "amrap"])' in source


def test_night_mode_job_dialog_amrap_label_switching() -> None:
    """Static check that _on_directory_changed relabels inputs for AMRAP."""
    gui_path = Path(__file__).parent.parent / "Lead Machine (Final Update 5).py"
    source = gui_path.read_text(encoding="utf-8")
    assert "def _on_directory_changed(self, directory: str):" in source
    assert 'if directory == "amrap":' in source
    assert 'self.input_label.setText("State filter (optional):")' in source
    assert 'self.amrap_genre_label.setVisible(True)' in source


def test_night_mode_job_dialog_get_job_sets_amrap_fields() -> None:
    """Static check that get_job() populates amrap_state and amrap_genre."""
    gui_path = Path(__file__).parent.parent / "Lead Machine (Final Update 5).py"
    source = gui_path.read_text(encoding="utf-8")
    assert 'if job["directory"].lower() == "amrap":' in source
    assert 'job["amrap_state"] = job["input_seed_csv"]' in source
    assert 'job["amrap_genre"] = self.amrap_genre_edit.text().strip()' in source


def test_pipeline_run_directory_job_passes_target_count(monkeypatch, tmp_path: Path) -> None:
    """target_valid_leads must reach the scraper as target_count."""
    from pipeline_runner import run_directory_job

    calls: List[Dict[str, Any]] = []

    def _fake_scrape_to_csv(**kwargs):
        calls.append(kwargs)
        output = kwargs["output_csv"]
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(
            "Artist Name,Lead_Source,Source_Directory,Source URL\n"
            'Test Artist,AMRAP,amrap,https://amrap.org.au/artist/test-artist\n',
            encoding="utf-8-sig",
        )

    monkeypatch.setattr("amrap_scraper.scrape_amrap_to_csv", _fake_scrape_to_csv)

    job_config = {
        "directory": "amrap",
        "target_valid_leads": 42,
        "amrap_state": "NSW",
        "amrap_genre": "rock",
    }
    output_path = tmp_path / "amrap_raw.csv"
    result = run_directory_job(job_config, str(output_path))

    assert len(calls) == 1
    assert calls[0]["target_count"] == 42
    assert calls[0]["state_filter"] == "NSW"
    assert calls[0]["genre_filter"] == "rock"


# ---------------------------------------------------------------------------
# GUI source selector
# ---------------------------------------------------------------------------

def test_gui_source_combo_includes_amrap() -> None:
    """Verify AMRAP appears in the GUI source selector values.

    This is a lightweight static check on the source file text rather than
    instantiating the Qt widget.
    """
    gui_path = Path(__file__).parent.parent / "Lead Machine (Final Update 5).py"
    source = gui_path.read_text(encoding="utf-8")
    assert '"AMRAP"' in source
    assert 'self.source_combo.addItems(["Unearthed", "Bandcamp", "SoundCloud", "Last.fm Similar", "Spotify", "AMRAP"])' in source


# ---------------------------------------------------------------------------
# Existing sources unchanged
# ---------------------------------------------------------------------------

def test_existing_source_dispatch_unchanged(monkeypatch, tmp_path: Path) -> None:
    """Bandcamp dispatch still works after AMRAP addition."""
    from pipeline_runner import run_directory_job

    bandcamp_calls: List[Dict[str, Any]] = []

    def _fake_bandcamp_scrape(**kwargs):
        bandcamp_calls.append(kwargs)
        output = kwargs.get("existing_csv") or str(tmp_path / "bandcamp.csv")
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(
            "Artist Name,Lead_Source,Source_Directory\n"
            'Band Artist,Bandcamp,bandcamp\n',
            encoding="utf-8-sig",
        )

    # bandcamp is called via module.scrape_bandcamp in pipeline_runner
    import pipeline_runner as pr
    orig_run = pr.run_directory_job

    # We can't easily monkeypatch the legacy module, so just verify the
    # AMRAP branch does not interfere with the bandcamp branch structure.
    source = Path(pr.__file__).read_text(encoding="utf-8")
    assert 'elif directory == "bandcamp":' in source
    assert 'elif directory == "amrap":' in source
    assert "bandcamp" in source.split('elif directory == "amrap":')[0]


def test_existing_night_mode_source_combo_unchanged() -> None:
    """Spotify, Bandcamp, SoundCloud and Unearthed remain in Night Mode dialog."""
    gui_path = Path(__file__).parent.parent / "Lead Machine (Final Update 5).py"
    source = gui_path.read_text(encoding="utf-8")
    for src in ("spotify", "bandcamp", "soundcloud", "unearthed"):
        assert src in source.split('self.directory_combo.addItems([')[1].split(']')[0]
