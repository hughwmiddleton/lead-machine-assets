from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde


def test_domain_org_export_rows_are_deterministic_and_exact():
    profile_index = {
        "zeta.com": {
            "contacts": ["bookings@zeta.com", "info@zeta.com"],
            "artist_count": 1,
            "artists_sample": ["Zeta One"],
            "source_types": ["facebook_enrich", "website_enrich"],
            "first_source_url": "https://zeta.com/contact",
            "seen_count": 1,
            "contact_counts": {
                "info@zeta.com": 1,
                "bookings@zeta.com": 2,
            },
            "org_type": "unknown",
            cde.DOMAIN_PROFILE_CONTACT_META_KEY: {
                "bookings@zeta.com": {"source_type": "facebook_enrich", "role": "booking"},
                "info@zeta.com": {"source_type": "website_enrich", "role": "general"},
            },
            "_artist_keys": {"zeta one"},
        },
        "alpha.com": {
            "contacts": ["contact@alpha.com", "mgmt@alpha.com"],
            "artist_count": 2,
            "artists_sample": ["Alpha One", "Alpha Two"],
            "source_types": ["facebook_enrich", "website_enrich"],
            "first_source_url": "https://alpha.com/contact",
            "seen_count": 2,
            "contact_counts": {
                "contact@alpha.com": 1,
                "mgmt@alpha.com": 2,
            },
            "org_type": "management",
            cde.DOMAIN_PROFILE_CONTACT_META_KEY: {
                "contact@alpha.com": {"source_type": "website_enrich", "role": "general"},
                "mgmt@alpha.com": {"source_type": "facebook_enrich", "role": "management"},
            },
            "_artist_keys": {"alpha one", "alpha two"},
        },
    }
    reuse_index = {
        "zeta.com": {
            "email": "bookings@zeta.com",
            "email_all": "info@zeta.com;bookings@zeta.com",
            "source_type": "facebook_enrich",
            "source_url": "https://facebook.com/zeta",
            "extract_method": "regex",
            "email_type": "fb_enrich",
            "role": "booking",
        },
        "alpha.com": {
            "email": "mgmt@alpha.com",
            "email_all": "mgmt@alpha.com;contact@alpha.com",
            "source_type": "website_enrich",
            "source_url": "https://alpha.com/contact",
            "extract_method": "regex",
            "email_type": "website_enrich",
            "role": "management",
        },
    }

    rows = cde._build_domain_org_export_rows(profile_index, reuse_index)

    assert rows == [
        {
            "domain": "alpha.com",
            "org_type": "management",
            "artist_count": 2,
            "primary_email": "mgmt@alpha.com",
            "emails": "contact@alpha.com;mgmt@alpha.com",
            "roles_seen": "management|general",
            "sources_seen": "website_enrich|facebook_enrich",
        },
        {
            "domain": "zeta.com",
            "org_type": "unknown",
            "artist_count": 1,
            "primary_email": "bookings@zeta.com",
            "emails": "bookings@zeta.com;info@zeta.com",
            "roles_seen": "booking|general",
            "sources_seen": "website_enrich|facebook_enrich",
        },
    ]


def test_domain_org_export_rows_handle_reuse_without_profile():
    rows = cde._build_domain_org_export_rows(
        {},
        {
            "brightmusic.com": {
                "email": "mgmt@brightmusic.com",
                "email_all": "mgmt@brightmusic.com;outside@test.com",
                "source_type": "facebook_enrich",
                "source_url": "https://facebook.com/brightmusic",
                "extract_method": "regex",
                "email_type": "fb_enrich",
                "role": "management",
            }
        },
    )

    assert rows == [
        {
            "domain": "brightmusic.com",
            "org_type": "unknown",
            "artist_count": 0,
            "primary_email": "mgmt@brightmusic.com",
            "emails": "mgmt@brightmusic.com",
            "roles_seen": "management",
            "sources_seen": "facebook_enrich",
        }
    ]


def test_domain_org_sidecar_skips_cleanly_when_empty(tmp_path: Path):
    output_csv = tmp_path / "master_enriched.csv"
    output_csv.write_text("Artist Name\n", encoding="utf-8-sig")
    logs = []

    sidecar_path = cde._write_domain_org_sidecar(
        output_csv.as_posix(),
        {},
        {},
        log_fn=logs.append,
    )

    assert sidecar_path == ""
    assert not Path(cde._domain_org_index_path(output_csv.as_posix())).exists()
    assert logs == ["[DomainOrg] No exportable domain/org data; sidecar skipped."]


def test_domain_org_sidecar_write_does_not_mutate_main_output_csv(tmp_path: Path):
    output_csv = tmp_path / "master_enriched.csv"
    original_df = pd.DataFrame([{"Artist Name": "Atlas Bloom", "Email": "atlas@example.com"}])
    original_df.to_csv(output_csv, index=False)
    before = output_csv.read_text(encoding="utf-8")

    cde._write_domain_org_sidecar(
        output_csv.as_posix(),
        {
            "brightmusic.com": {
                "contacts": ["mgmt@brightmusic.com"],
                "artist_count": 2,
                "artists_sample": ["Bright One", "Bright Two"],
                "source_types": ["website_enrich"],
                "first_source_url": "https://brightmusic.com/contact",
                "seen_count": 2,
                "contact_counts": {"mgmt@brightmusic.com": 2},
                "org_type": "management",
                cde.DOMAIN_PROFILE_CONTACT_META_KEY: {
                    "mgmt@brightmusic.com": {"source_type": "website_enrich", "role": "management"}
                },
                "_artist_keys": {"bright one", "bright two"},
            }
        },
        {
            "brightmusic.com": {
                "email": "mgmt@brightmusic.com",
                "email_all": "mgmt@brightmusic.com",
                "source_type": "website_enrich",
                "source_url": "https://brightmusic.com/contact",
                "extract_method": "regex",
                "email_type": "website_enrich",
                "role": "management",
            }
        },
    )

    after = output_csv.read_text(encoding="utf-8")
    sidecar_df = pd.read_csv(cde._domain_org_index_path(output_csv.as_posix()), dtype=str, keep_default_na=False)

    assert before == after
    assert list(sidecar_df.columns) == list(cde.DOMAIN_ORG_SIDECAR_COLUMNS)
    assert sidecar_df.to_dict(orient="records") == [
        {
            "domain": "brightmusic.com",
            "org_type": "management",
            "artist_count": "2",
            "primary_email": "mgmt@brightmusic.com",
            "emails": "mgmt@brightmusic.com",
            "roles_seen": "management",
            "sources_seen": "website_enrich",
        }
    ]
