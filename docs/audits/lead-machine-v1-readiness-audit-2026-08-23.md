# Lead Machine V1 Readiness Audit

**Audit date:** 2026-08-23  
**Branch:** `audit/lead-machine-v1`  
**Baseline commit:** `727165eeb8bd3fc54dcaae9d4b060ef99be8fc85`  
**Auditor:** Kimi Code CLI (read-only)  
**Scope:** Full architecture, simplification, and V1 readiness audit  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current Architecture](#2-current-architecture)
3. [End-to-End Pipeline](#3-end-to-end-pipeline)
4. [Subsystem Inventory](#4-subsystem-inventory)
5. [Source-by-Source Audit](#5-source-by-source-audit)
6. [Night Mode Audit](#6-night-mode-audit)
7. [Identity / Deduplication / Lead Vault](#7-identity--deduplication--lead-vault)
8. [Enrichment & Facebook Dependency](#8-enrichment--facebook-dependency)
9. [Contact Discovery](#9-contact-discovery)
10. [Validation](#10-validation)
11. [Export / Campaign Contract](#11-export--campaign-contract)
12. [Operator Workflow](#12-operator-workflow)
13. [Security / Operational Risks](#13-security--operational-risks)
14. [Test & Confidence Assessment](#14-test--confidence-assessment)
15. [Legacy / Dead / Duplicate Architecture](#15-legacy--dead--duplicate-architecture)
16. [Simplification Opportunities](#16-simplification-opportunities)
17. [V1 Launch Blockers](#17-v1-launch-blockers)
18. [Manual-Assistance Procedures](#18-manual-assistance-procedures)
19. [Post-V1 Work](#19-post-v1-work)
20. [Recommended Target V1 Architecture](#20-recommended-target-v1-architecture)
21. [Prioritized Action Plan](#21-prioritized-action-plan)
22. [Final V1 Readiness Verdict](#22-final-v1-readiness-verdict)

---

## 1. Executive Summary

Lead Machine is a mature but **overgrown** artist-discovery-and-enrichment system. It can discover artists from six sources (Unearthed, Bandcamp, Spotify, SoundCloud, Last.fm, festivals), enrich them with contact information (Facebook, Instagram, websites), validate provenance, deduplicate, and export campaign-ready CSVs. It has a PyQt5 GUI, a Night Mode unattended orchestration layer, and a Lead Vault master dataset.

**The system is usable for V1 with manual assistance, but it carries significant operational risk and unnecessary complexity.**

### Key findings

- **Launchable with Hugh operating manually:** Yes. The core discovery and export paths work. Night Mode v2 provides a reasonable unattended orchestration layer.
- **Facebook is not architecturally required** but is the highest-yield email source. The system correctly preserves no-email rows.
- **Security is the most urgent concern:** Hardcoded API keys, a committed Chrome browser profile with session cookies, and committed PII (emails, names, locations) in CSVs/logs are all in the repository.
- **Complexity is the second concern:** Three massive files (`Lead Machine (Final Update 5).py` 16.5k lines, `cross_directory_enricher.py` 21.6k lines, `night_mode_fb.py` 11.3k lines) contain overlapping responsibilities. Legacy and current code coexist. Deduplication, email consolidation, and export bridging logic are duplicated across modules.
- **Night Mode v2 is the right orchestration layer** but it delegates to v1 helpers and a monolithic Facebook enricher. It should be consolidated, not rebuilt.
- **Lead Vault is a god object** doing schema, import, merge, scoring, origin locking, and export in one package. It is functional but over-complicated for V1.
- **Validation is performed 4–5 times** across the pipeline. Some steps (origin HTML fetching, staleness downgrade) block or downgrade otherwise-good leads unnecessarily.

### Verdict

> **YES, AFTER SMALL FIXES** — The system can serve real Studiflow customers next week if Hugh operates it manually and fixes exceptions as they arise. The "small fixes" are primarily **security hygiene** (rotate keys, purge PII from git, fix `.gitignore`) and **a few operational guardrails** (Spotify playlist configuration, SoundCloud client ID rotation, Night Mode FB captcha handling). Architectural simplification should wait until after V1 is generating revenue.

---

## 2. Current Architecture

### Top-level structure

```text
/Users/hughmiddleton/Lead Machine/Lead Machine VS Code/lead-machine-v1-audit
├── Lead Machine (Final Update 5).py   # 16,514 lines — PyQt5 GUI + legacy scrapers
├── cross_directory_enricher.py        # 21,585 lines — master enrichment orchestrator
├── night_mode_fb.py                   # 11,333 lines — Night Mode Facebook enricher
├── pipeline_runner.py                 # 5,266 lines — pipeline orchestration layer
├── night_mode_runner.py               # 2,088 lines — Night Mode v1 orchestration
├── source_scheduler.py                # 956 lines — round-robin source scheduler
├── spotify_scraper.py                 # 986 lines — Spotify discovery
├── soundcloud_engine.py               # 1,728 lines — SoundCloud discovery
├── facebook_enrich.py                 # 2,159 lines — legacy daytime FB enricher
├── soundcloud_metadata_enricher.py    # 730 lines — SC genre/release enrichment
├── spotify_about_scraper.py           # 635 lines — Spotify About-page enrichment
├── website_email_scraper.py           # 300 lines — website email extraction
├── html_fetcher.py                    # 304 lines — HTTP + Playwright fallback
├── email_normalizer.py                # 3,687 lines — email normalization
├── email_provenance.py                # 446 lines — email provenance JSON tracking
├── fb_attribution.py                  # 301 lines — FB/IG attribution columns
├── fb_email_override.py               # 172 lines — FB candidate override logic
├── fb_email_skip_gate.py              # 101 lines — skip FB if row has email
├── origin_validator.py                # 786 lines — source-page origin validation
├── final_checker.py                   # 428 lines — post-enrichment quality checker
├── festival_scraper.py                # 256 lines — festival lineup scraper
├── unearthed_common.py                # 137 lines — Unearthed genre helpers
├── lead_vault/                        # 8 modules — master CSV ETL/dedupe/export
├── night_mode_v2/                     # 7 modules — phased Night Mode architecture
├── scripts/                           # 16 diagnostic/recovery scripts
├── tests/                             # 130+ test files
├── data/                              # master CSVs and backups
├── overnight_runs/                    # historical run outputs
└── .../                               # committed Chrome profile (DANGER)
```

### Primary runtime entry points

| Entry Point | File | Line | Role |
|-------------|------|------|------|
| **GUI** | `Lead Machine (Final Update 5).py` | 16460 | PyQt5 application; default entry point |
| **Night Mode CLI** | `night_mode_runner.py` | 2063 | `main()` → calls `run_phased_night_mode` from `night_mode_v2` |
| **Pipeline wrapper** | `pipeline_runner.py` | 3410+ | `run_master_enrichment`, `run_directory_job`, `run_enrichment`, `run_facebook_global_pass_nightmode` |
| **Spotify OAuth setup** | `spotify_oauth_setup.py` | 29 | One-time token helper |
| **Shell launcher** | `run_lead_machine.sh` | 1 | Sets env vars and launches GUI |

### Major subsystems

1. **GUI Layer** (`Lead Machine (Final Update 5).py`) — Artist Scraping, Facebook Scraping, Cross-Directory Enricher, Lead Vault, Campaign Prep, Auto-Validate, Night Mode tabs.
2. **Pipeline Runner** (`pipeline_runner.py`) — Thin wrapper around scrapers and enrichment; resume/checkpoint logic; export.
3. **Source Adapters** — Unearthed, Bandcamp, Spotify, SoundCloud, Last.fm, Festival scrapers.
4. **Cross-Directory Enricher** (`cross_directory_enricher.py`) — Master enrichment: directory matching, live search, website email, Instagram, Facebook.
5. **Night Mode** (`night_mode_runner.py` + `night_mode_v2/` + `night_mode_fb.py`) — Unattended multi-source orchestration, FB global pass, merge/dedupe.
6. **Lead Vault** (`lead_vault/`) — Canonical master CSV, deduplication, merge strategies, export presets.
7. **Validation** (`origin_validator.py`, `final_checker.py`) — Origin HTML verification, duplicate/conflict flagging, status computation.
8. **Email & Provenance** (`email_provenance.py`, `email_normalizer.py`, `fb_attribution.py`) — Email priority, provenance JSON, platform attribution columns.

### Active vs historical/deprecated code

| File | Lines | Status |
|------|-------|--------|
| `Lead Machine (Final Update 5).py` | 16,514 | Legacy but still required — dynamically imported by pipeline_runner and tests |
| `cross_directory_enricher.py` | 21,585 | Current but overcomplex — god module |
| `night_mode_fb.py` | 11,333 | Current but overcomplex — Night Mode FB runtime |
| `facebook_enrich.py` | 2,159 | Likely dead — superseded by `night_mode_fb.py`; only in tests/recovery scripts |
| `night_mode_v2/` | ~650 | Current — v2 is the active Night Mode entrypoint |
| `discover.js` | 50 lines | Likely dead — no Python imports |
| `spotify_oauth_setup.py` | 105 | Current but fragile — hardcoded credentials |

---

## 3. End-to-End Pipeline

### Conceptual pipeline

```text
Source / Seed
    ↓
Source Discovery (per-directory scrape)
    ↓
Per-directory raw CSV
    ↓
Per-directory enrichment (origin validation + final checker)
    ↓
Per-directory enriched CSV
    ↓
Master raw merge (concat + dedupe)
    ↓
Master enrichment (cross-directory matching + live search + website/IG/FB)
    ↓
Master pre-FB CSV
    ↓
Facebook global pass (optional, Night Mode)
    ↓
Master post-FB CSV
    ↓
Final enrichment / recompute status
    ↓
Master export (filter + dedupe + campaign schema)
    ↓
Campaign-ready CSV
```

### Actual pipeline (as implemented)

#### Daytime / GUI path

```text
User selects source + parameters in GUI
    ↓
ArtistScraperThread.run() → scrape_website / scrape_bandcamp / scrape_soundcloud /
                          scrape_lastfm_similar / scrape_spotify
    ↓
Per-source CSV written directly
    ↓
(Optional) AutoValidateWorker → origin_validator.run_auto_validate
    ↓
(Optional) FacebookScraperThread → scrape_csv (legacy FB scraper)
    ↓
(Optional) CrossDirectoryEnricherWorker → run_cross_directory_enrichment
    ↓
(Optional) LeadVaultTab → merge/import into master CSV
    ↓
(Optional) CampaignPrepTab → split/format/export campaign CSVs
```

#### Night Mode path (v2)

```text
overnight_jobs.json config
    ↓
night_mode_runner.py main() → run_phased_night_mode (night_mode_v2/phased_runner.py)
    ↓
Phase: SEED
    For each job:
        pipeline_runner.run_directory_job() → raw.csv
        pipeline_runner.run_enrichment() → enriched.csv
    ↓
Phase: ENRICH
    _merge_raw_master() → master_raw.csv
    pipeline_runner.run_master_enrichment() → master_enriched.csv
    pipeline_runner.run_enrichment() → master_pre_fb.csv
    ↓
Phase: CONTACT
    pipeline_runner.run_facebook_global_pass_nightmode() → master_post_fb.csv
    pipeline_runner.run_enrichment() → master_final.csv
    pipeline_runner.export_master_leads() → master_export_leads.csv
```

### Stage details

| Stage | Owning Module | Inputs | Outputs | Side Effects | Retry / Fallback |
|-------|--------------|--------|---------|--------------|------------------|
| **Source Discovery** | `pipeline_runner.run_directory_job` | Job config JSON | `raw.csv` per job | Browser automation, HTTP requests, temp files | Per-source: Spotify zero-row fallback chain (Bandcamp → SoundCloud → etc.); Bandcamp empty-streak termination |
| **Per-Job Enrichment** | `pipeline_runner.run_enrichment` | `raw.csv` | `enriched.csv` | Origin HTML fetching, final checker | Falls back to copy if auto-validate fails |
| **Master Merge** | `night_mode_runner._merge_raw_master` | Per-job enriched CSVs | `master_raw.csv` | DataFrame concat, email coalescing, FB URL promotion, dedupe | None |
| **Master Enrichment** | `pipeline_runner.run_master_enrichment` | `master_raw.csv` + directory CSVs | `master_enriched.csv` | Cross-directory matching, live search (Bandcamp/Last.fm), website/IG/FB enrichment | Falls back to seed copy on failure |
| **FB Global Pass** | `pipeline_runner.run_facebook_global_pass_nightmode` | `master_pre_fb.csv` | `master_post_fb.csv` | Selenium driver, Facebook page visits, email extraction | Resume checkpoint per row; auto-resume after captcha; driver recovery |
| **Final Export** | `pipeline_runner.export_master_leads` | `master_final.csv` | `master_export_leads.csv` | Origin integrity repair, email consolidation, status recompute, filter | None |

### Duplicated logic across stages

- **Email consolidation (`Email_All`)**: Called in `run_enrichment`, `run_facebook_global_pass_nightmode`, `export_master_leads`, and multiple Lead Vault merge helpers.
- **Auto-validate**: Called directly from GUI `AutoValidateTab`, from `pipeline_runner.run_enrichment`, and implicitly during Night Mode.
- **Facebook URL promotion**: Exists in `source_scheduler.py`, `night_mode_runner.py`, `pipeline_runner.py`, and `cross_directory_enricher.py`.
- **Origin integrity repair**: Called in `lead_vault/exporter.py`, `pipeline_runner.export_master_leads`, and `lead_vault/origin.py`.
- **Final status recompute**: Called in `pipeline_runner.run_enrichment`, `pipeline_runner.export_master_leads`, and the legacy final export bridge.

---

## 4. Subsystem Inventory

| Subsystem | Files | Responsibility | V1 Essential? |
|-----------|-------|----------------|---------------|
| **GUI** | `Lead Machine (Final Update 5).py` | Operator interface, direct scraper invocation | Yes — manual V1 requires GUI |
| **Pipeline Runner** | `pipeline_runner.py` | Orchestration, checkpoints, resume, export | Yes — Night Mode depends on it |
| **Night Mode v2** | `night_mode_v2/`, `night_mode_runner.py` | Phased unattended orchestration | Yes — for overnight campaigns |
| **Night Mode FB** | `night_mode_fb.py` | Facebook enrichment in Night Mode | Yes — highest email yield |
| **Cross-Directory Enricher** | `cross_directory_enricher.py` | Master enrichment, live search, IG, website, FB | Yes — for multi-source merging |
| **Lead Vault** | `lead_vault/` | Master CSV, dedupe, merge, export presets | Yes — canonical dataset |
| **Origin Validator** | `origin_validator.py` | Source-page HTML verification | Partial — can be async/manual |
| **Final Checker** | `final_checker.py` | Quality flags, duplicate detection, status | Yes — but should be simplified |
| **Email Provenance** | `email_provenance.py` | Per-email source tracking | Yes — prevents bad overwrites |
| **Source Scheduler** | `source_scheduler.py` | Round-robin enrichment scheduling | Partial — only used by cross-directory enricher |
| **Spotify Scraper** | `spotify_scraper.py`, `spotify_client.py`, `spotify_about_scraper.py` | Spotify discovery + enrichment | Yes |
| **SoundCloud Engine** | `soundcloud_engine.py`, `soundcloud_metadata_enricher.py` | SoundCloud discovery + enrichment | Yes |
| **Bandcamp Scraper** | `Lead Machine (Final Update 5).py` (scrape_bandcamp) | Bandcamp discovery | Yes |
| **Unearthed Scraper** | `Lead Machine (Final Update 5).py` (scrape_website) | Unearthed discovery | Yes |
| **Last.fm Scraper** | `Lead Machine (Final Update 5).py` (scrape_lastfm_similar) | Last.fm similar artists | Partial — good for seed expansion |
| **Festival Scraper** | `festival_scraper.py` | Festival lineup seed | No — defer |
| **Legacy FB Enricher** | `facebook_enrich.py` | Daytime FB scraping | No — superseded by night_mode_fb |

---

## 5. Source-by-Source Audit

### 5.1 Triple J Unearthed

**Files:** `Lead Machine (Final Update 5).py:2593` (`scrape_website`), `3343` (`scrape_artist_profile`)

| Aspect | Detail |
|--------|--------|
| **Discovery** | Selenium browser automation against `abc.net.au/triplejunearthed`. Two modes: live listing ("Load More" button clicking) or indexed URL mode (local JSON/CSV index). |
| **Seed** | Unearthed listing URL, or pre-built index file. Resume cursor supports `auto` / `cursor` / `fresh` / `selected`. |
| **Pagination** | Infinite scroll via XPath `//button[contains(...,'load more')]`. Up to 3 attempts with scroll-into-view validation. |
| **Fields captured** | Artist Name, Location, Played on triple J / Unearthed, Social Links (FB, IG, Twitter, TikTok, YouTube, Spotify, SoundCloud), Song Title, Sounds Like, Release Date, Primary Genre, Email (optional regex on page source). |
| **Enrichment** | Facebook email scraping if `fb_driver` available; metadata merge from listing card to profile page. |
| **Reliability** | Hashed CSS class selectors (`HU3iy.p1_Ju.mqDRk.FQED6.O_grP`) are brittle. Resume cursor depends on exact profile URL match in dynamically loaded stream — breaks if listing order changes. No explicit rate limiting. |
| **Code quality** | `scrape_website` is ~700+ lines with deeply nested closures, heavy `nonlocal` usage, mixed responsibilities. Hardcoded ABC-owned social URL exclude list (l.3353). |
| **V1 Verdict** | **READY WITH MANUAL ASSISTANCE** — Use indexed-URL mode for production; live listing mode too fragile for unattended runs. |

### 5.2 Bandcamp

**Files:** `Lead Machine (Final Update 5).py:5359` (`scrape_bandcamp`), `6019` (discover/tag/search)

| Aspect | Detail |
|--------|--------|
| **Discovery** | Selenium for listing pages, requests fallback for direct profile fetches. Three modes: `discover`, `tag`, `search`. |
| **Seed** | Tag strings, Bandcamp URL, or search query + optional location filter. |
| **Pagination** | Page-based URLs (`?page=N`) for discover/tag. Search mode uses infinite-scroll "View more results" fallback. Empty-streak termination after 3 zero-candidate pages. |
| **Fields captured** | Artist name, location, genres, primary genre, sounds like, latest release title/date, website, email (bio regex), socials (IG, Twitter, FB, YouTube, Linktree, Spotify, Bandsintown, Songkick). |
| **Enrichment** | Release page deep-dive for date precision. Location filtering. Search cutoff (releases >730 days rejected). |
| **Reliability** | 8 different grid selectors (`_BANDCAMP_GRID_SELECTORS`) to handle inconsistent markup. Profile parsing relies on regex class names and long bio/contact selector lists. |
| **Code quality** | Deep nesting for URL kind detection, mode normalization, smoke caps. Checkpoint drift risk (JSON saved every 5 artists may get ahead of CSV). Significant overlap between tag-page and mode-page collection. |
| **V1 Verdict** | **READY WITH MANUAL ASSISTANCE** — Tag and search modes are stable. Discover mode more fragile due to infinite-scroll variance. |

### 5.3 Spotify

**Files:** `spotify_scraper.py`, `spotify_client.py`, `spotify_about_scraper.py`, `spotify_playlist_html_scraper.py`

| Aspect | Detail |
|--------|--------|
| **Discovery** | Spotify Web API (client credentials) for playlist track enumeration. Playwright-based HTML fallback if API 404s. |
| **Seed** | Spotify playlist IDs/URLs or search term containing playlist URL. |
| **Pagination** | API: offset/limit (100 per page). HTML fallback: mouse wheel scroll up to 60 attempts. |
| **Fields captured** | Artist Name, Spotify URL, Spotify Artist ID, Song Title, Spotify Playlist URL, Spotify Seed Position, Spotify Genres, Spotify Followers, Spotify Popularity. |
| **Enrichment** | About-page enrichment (`__NEXT_DATA__` JSON parsing for external links, city, genres). Website → email enrichment. Genre fallback chain (related artists → top tracks → Last.fm tags → directory CSV genre map). |
| **Reliability** | API auth is robust with token refresh. `__NEXT_DATA__` is internal Next.js payload — can change. HTML fallback `data-testid` attributes are relatively stable but not guaranteed. |
| **Code quality** | `TODO_FRESH_FINDS_*_PLAYLIST_ID` placeholders still present (l.29). Cross-module implicit dependency on other sources' output filenames for genre mapping. Private method access with `# type: ignore`. |
| **V1 Verdict** | **READY** — API path is solid. HTML fallback and about-page enrichment are acceptable safety nets. Main risk is credential expiry. |

### 5.4 SoundCloud

**Files:** `soundcloud_engine.py`, `soundcloud_metadata_enricher.py`

| Aspect | Detail |
|--------|--------|
| **Discovery** | SoundCloud v2 API (`api-v2.soundcloud.com/search/users`). Client ID resolved dynamically from hardcoded candidates or JS scraping fallback. |
| **Seed** | Artist name/keyword query + optional location filter (`filter.place`). |
| **Pagination** | API: `linked_partitioning` with `offset` driven by `next_href`. |
| **Fields captured** | Handle, display name, city, country, genre, external URLs, emails (mailto + bio regex), website, bio text, sounds like, latest track title/date/genre/tags. |
| **Enrichment** | About page → root page → API fallback → RSS fallback (`feeds.soundcloud.com/.../sounds.rss`). Aggregator expansion (Linktree/Beacons). Metadata enricher for genre/release date. |
| **Reliability** | Hardcoded client IDs expire. Tracks endpoint aggressively returns 401/403; RSS fallback mitigates but provides less data. Challenge-page circuit breaker disables about-page fetching if >60% hit rate — good defense but causes data loss. |
| **Code quality** | Heavy module-level globals (`_SC_ABOUT_DISABLED`, `_SC_RUN_STATS`, etc.) with locks. 1728 lines in single file with mixed concerns. Duplicated genre regex patterns with Last.fm/Spotify. |
| **V1 Verdict** | **READY WITH MANUAL ASSISTANCE** — Sophisticated fallback chain but requires monitoring due to anti-bot circuit breaker. |

### 5.5 Last.fm

**Files:** `Lead Machine (Final Update 5).py:3662` (`scrape_lastfm_similar`)

| Aspect | Detail |
|--------|--------|
| **Discovery** | Last.fm REST API (`artist.getSimilar`, `artist.getInfo`, `artist.getTopTracks`). |
| **Seed** | List of artist name strings. `LASTFM_API_KEY` required. |
| **Pagination** | API-level `limit` param (default ~200 per seed). No offset. |
| **Fields captured** | Artist Name, Primary Genre (first tag), Song Title (first top track), Sounds Like (static string). |
| **Enrichment** | HTML page scrape for website, socials, location. |
| **Reliability** | Entirely non-functional without API key. HTML parsing uses simple class regex. Data often sparse — socials frequently link back to Last.fm or Wikipedia. No rate limiting. |
| **Code quality** | Minimal error handling (API failures return `{}` silently). Duplicated genre logic with Spotify. Tight coupling to CSV writing inside scraper. |
| **V1 Verdict** | **READY WITH MANUAL ASSISTANCE** — Best used as seed expansion / genre fallback, not as primary contact source. |

### 5.6 Festival Scraper

**Files:** `festival_scraper.py`

| Aspect | Detail |
|--------|--------|
| **Discovery** | Static HTML scraping for 4 hardcoded festivals (Bigsound, SXSW, Great Escape, Laneway). |
| **Seed** | Festival keys list. |
| **Pagination** | None — single-page fetch. |
| **Fields captured** | Artist Name, Festival Sources, Festival Count, Seed Priority. |
| **Enrichment** | Cross-festival deduplication only. Zero contact data. |
| **Reliability** | Extremely brittle selectors (`.artist-card h3`, `.lineup-item h3`). No fallback selectors. |
| **V1 Verdict** | **DEFER** — Too narrow and brittle for V1 production. |

---

## 6. Night Mode Audit

### What is Night Mode?

Night Mode is an **unattended orchestration layer** that runs multiple Lead Machine directory scrapes sequentially, enriches each CSV, merges them into a deduplicated master, runs a Facebook global pass for contact discovery, and produces a final export. It solves the problem of running multi-source campaigns (e.g., Spotify + Bandcamp + SoundCloud + Unearthed) overnight without human intervention.

### Night Mode v1 vs v2

- **v1** (`night_mode_runner.py:run_night_mode`) — Monolithic loop: per-job scrape → per-job enrichment → master merge → master enrichment → FB global pass → final export. Still exists but **no longer invoked from `main()`**.
- **v2** (`night_mode_v2/phased_runner.py:run_phased_night_mode`) — Active entrypoint. Three explicit phases with a `run_manifest_v2.json`:
  1. **Seed** — scrape all jobs
  2. **Enrich** — merge raw → master enrichment → validation → quarantine
  3. **Contact** — FB global pass → final validation → export

v2 reuses v1 helpers (`_merge_raw_master`, smoke stats, quarantine) and delegates scraping/enrichment/FB to `pipeline_runner`. It is a **manifest-driven state machine**, not a duplicate runtime.

### Sources supported

All major sources: Spotify, Bandcamp, SoundCloud, Last.fm, Unearthed.

### Facebook handling in Night Mode

Night Mode uses `night_mode_fb.py` (`NightModeFacebookEnricher`) rather than the daytime `facebook_enrich` module directly:

- **Persistent Chrome profile** session reuse across rows
- **Trust budget / health monitoring** — degrades or disables FB on checkpoints/captchas
- **Two-pass execution** — Pass A (direct canonical URLs), Pass B (discovery search)
- **Music-bias scoring** — musician/band descriptors score higher
- **Pacing** — per-row delays, short/long breaks, page budgets, slow-mode on errors
- **Email extraction** — raw HTML regex + rendered text + anchor scan + reveal clicks + obfuscation normalization

### Resume and failure handling

- **Per-job resume**: `state.json` tracks `status`; completed jobs skipped on `--resume`.
- **v2 manifest resume**: `run_manifest_v2.json` tracks phase outputs; skip logic requires matching config hash + existing outputs + schema validity.
- **FB row-level resume**: `pipeline_runner.build_resume_checkpoint()` creates monotonic row checkpoint files.
- **Failure behavior**: Per-job `MAX_CONSECUTIVE_ERRORS = 10` before abort. FB pass falls back to `master_pre_fb` on failure. Spotify zero rows triggers fallback chain rather than hard failure.

### Provenance preservation

- `__source_job` column tracks originating job
- Email coalescing backfills `Email_All` from `Email`
- Smear Guard detects emails repeated across ≥5 rows / multiple jobs
- Quarantine clears repeated emails on non-origin rows so FB can attempt recovery
- Facebook attribution columns (`FB_Status`, `FB_Reason`, etc.) track FB attempt outcomes

### Final exports

Night Mode emits:
- `master_raw.csv` → `master_enriched.csv` → `master_pre_fb.csv` → `master_post_fb.csv` → `master_enriched_deduped.csv` → `master_export_leads.csv`
- Deduplication by email or `(Artist Name + URL)`, preferring rows with emails / Facebook clues
- Export presets: `studio_safe`, `studio_plus`, `unearthed_social`, `full_dump`

### Assessment

Night Mode is **(a) a useful orchestration layer that has grown complex and should be consolidated**. The v2 phased design is correct. The main problems are:

1. v1 `run_night_mode` still exists alongside v2 — should be removed once v2 fully subsumes it.
2. `night_mode_runner.py` is 2088 lines mixing orchestration, merge logic, email quarantine, smoke stats, export helpers, and CLI parsing.
3. `night_mode_fb.py` is 11,333 lines — an entire FB scraping runtime parallel to `facebook_enrich.py`.
4. Dataframe guards (quarantine, smear guard, dedupe) could be moved to `lead_vault/` or a shared module so normal mode can use them.
5. Checkpoint systems (row-level, job-level, manifest-level) are complementary but could be unified.

**Recommendation:** Keep v2 phased orchestration. Delete v1 monolithic loop. Promote `night_mode_fb.py` session logic into a shared FB session manager. Move dataframe guards into shared utilities. Unify checkpoints under the v2 manifest.

---

## 7. Identity / Deduplication / Lead Vault

### Identity model

Artist identity is **implicitly defined by merge keys**, not a single canonical ID:

- **Primary merge key:** `Source_URL` (normalized profile URL)
- **Fallback merge key:** `Artist` + `Location` composite
- **Schema ID fields** (`Artist_ID`, `Spotify_Artist_ID`) exist but are **not used in deduplication**.

### Deduplication

Two code paths in `lead_vault/merge.py`:

1. **Standard merge** (`_run_csv_merge`) — inverted indexes on `profile_url` and `artist_location`. `_select_match` looks up incoming rows. Ambiguous matches rejected.
2. **Consolidating merge** (`_run_consolidating_csv_merge`) — dictionary keyed by consolidation key. Higher-scored row wins via `_candidate_beats_current`.

### Merge logic risks

- **Facebook_URL**: Incoming **always wins** — worse FB URL can silently replace better one (`_merge_facebook_field`).
- **General fields**: If both existing and incoming are non-empty and not equivalent, **existing is kept, incoming silently dropped**.
- **Scoring system**: `+100` for `Primary_Email`, `+10` per `All_Emails` email, `+5` for FB/IG URLs. Coarse — can prefer breadth over accuracy.

### Provenance tracking

Three levels:
1. **Row-level origin fields** (`Lead_Source`, `Source_Directory`, etc.) — write-once locked by `OriginLockedRow`
2. **Email-level provenance** — `Email_Provenance_JSON` column with per-email `source_type`, `surface`, `source_url`, `extract_method`
3. **Origin validation** — `origin_validator.py` fetches source pages and verifies artist/track match with fuzzy scoring

### Lead Vault responsibilities

Lead Vault is a **god object** spanning 6+ responsibilities:
- Schema definition (`schema.py`)
- CSV I/O & encoding detection (`importer.py`)
- Header aliasing (`alias_map.py`)
- Deduplication & indexing (`merge.py`)
- Scoring & ranking (`merge.py`)
- Field-level merge logic (`merge.py` — 10+ field-specific helpers)
- Origin integrity locking (`origin.py`)
- Export formatting & bridging (`exporter.py`)
- Backup management (`merge.py`)

### Export paths

- **Woodpecker preset** (`lead_vault/exporter.py:21`) — 21 headers, filter `has_primary_email`
- **Final export preset** (`lead_vault/exporter.py:73`) — 30 headers including FB/IG state machine columns, uses `legacy_final_export_bridge`
- **`export_master_leads`** (`pipeline_runner.py:5217`) — reads CSV → repair origin → validate → consolidate email → normalize terminals → recompute status → filter → write `DEFAULT_EXPORT_COLUMNS`

**Duplication observed:** `repair_origin_integrity_df`, `recompute_final_status_post_enrichment`, and email consolidation logic exist in both the bridge and `export_master_leads`.

### Provenance at export

- `Lead_Source`, `Source_Directory`, `Discovery_Source`, `Source_URL` survive.
- `Email_Provenance_JSON` is **lossily compressed** into 3 string columns (`Email_Source_URL`, `Email_Source_Type`, `Email_Extract_Method`). The rich JSON map is dropped.

### Simplified conceptual identity model

Separate into four bounded contexts:
1. **Identity Core** — stable UUID/hash from normalized name + primary source URL; alias table for variant names and platform IDs
2. **Contact Core** — emails and social links as records with full provenance; derive `Primary_Email` / `All_Emails` at export time
3. **Source Core** — append-only ingestion events; single `source_origin` record instead of duplicated columns
4. **Quality & Export Views** — computed status/flags as projections over normalized data; export presets as pure selects

---

## 8. Enrichment & Facebook Dependency

### Enrichment mechanisms

| Mechanism | Problem Solved | Source-Specific | Mandatory | Failure Behaviour |
|-----------|---------------|-----------------|-----------|-------------------|
| **Facebook Enrichment** | Find artist FB pages, extract emails | No | No | Skips row; attribution columns record why |
| **Instagram Email** | Extract email from IG profile / bio-link | No | No | Skips; attribution columns record blocked/unavailable |
| **Website Email** | Crawl artist websites for emails | No | No | Silent miss |
| **Spotify About-Page** | Enrich Spotify rows with website URLs | Yes (Spotify) | No | Silent skip |
| **SoundCloud Metadata** | Fill genre/release date from SC | Yes (SC) | No | Returns `None`; row stays usable |
| **Bandcamp/Last.fm Live Search** | Discover profiles/emails for unmatched artists | No | No | Breaker trips; adaptive cooldown |
| **Directory Cross-Match** | Match seed rows against pre-scraped CSVs | Yes | No | No match = no enrichment |

### Facebook deep dive

**Daytime path** (`cross_directory_enricher.py:9662`):
1. `FacebookSearchClient` (Selenium) ensures logged-in session
2. Searches FB via `homepage_ui` method
3. Extracts candidates from search DOM (`facebook_enrich.py:1700`)
4. Scores candidates (name match + category boost + corporate penalty)
5. Selects best candidate; rejects non-music pages
6. Navigates to page; regex-extracts emails

**Night Mode path** (`night_mode_fb.py`):
1. `NightModeFacebookEnricher` with persistent Chrome profile
2. Trust budget health system
3. Two-pass: direct URLs → discovery search
4. Multi-layer email extraction: raw HTML → rendered text → anchors → reveal clicks → obfuscation normalization

### Fallback chains

| Step | Trigger | Action |
|------|---------|--------|
| No explicit FB URL | Row lacks `Facebook_URL` | Trigger FB discovery search |
| Discovery search no candidates | Zero usable results | Slug fallback (`facebook.com/<artist>`) |
| Pass A fails | Login wall / no email | Stop — do not brute-force |
| Session unhealthy | Trust score ≤ -5 | Protective shutdown of FB for run |
| IG direct no email | `all_ig_emails` empty | One-hop bio-link follow |
| Website no email | No emails on homepage | Contact-page follow (`/contact`, `/about`, `/book`) |

### Is Facebook a single point of failure?

**Not architecturally required, but operationally dominant.**
- Rows without FB enrichment are preserved and exported.
- However, FB is the highest-yield email source in practice.
- Evidence of privilege: `fb_email_override.py` accepts FB candidates even when music signals are weak.
- `fb_email_skip_gate.py` explicitly skips rows that already have email — proving FB is not the sole source.

### Can Lead Machine produce useful leads without Facebook?

**Yes, but with reduced yield.**
- `website_email_scraper.py` is standalone and effective.
- Spotify rows get website URLs from About pages → website emails.
- Instagram has sophisticated multi-layer fallback.
- Directory matching provides pre-scraped emails without live FB.
- **Cleaner fallback chain conceptually:** Discovery → Website Email → Instagram → Directory Cross-Match → Manual Review. Facebook should be treated as a high-yield optional enrichment, not a requirement.

---

## 9. Contact Discovery

### Email sources (priority order)

From `email_provenance.py:30-44`:

1. `facebook_about` (highest)
2. `facebook_main`
3. `website_contact_page`
4. `website_homepage`
5. `instagram_profile`
6. `soundcloud_profile`
7. `bandcamp_contact_follow`
8. `bandcamp_profile`
9. `bandcamp_track_follow`
10. `lastfm_profile`
11. `spotify_profile`
12. `domain_reuse`
13. `live_search` (lowest)

### How emails are found

1. **Seed CSV / Directory data** — pre-existing emails from Bandcamp/SoundCloud/Last.fm/Unearthed
2. **Spotify About page** → artist websites → website email scraper
3. **Website scrape** — regex from HTML, contact-page follow, mailto detection
4. **Instagram** — direct profile HTML regex + bio-link one-hop + live rendered text
5. **Facebook** — page HTML + visible text + anchor scan + reveal clicks
6. **Live search** (Bandcamp / Last.fm) — search-by-name → profile HTML → email regex
7. **Domain reuse** — if multiple artists share a domain, email from one may be indexed for others

### What happens when no email is found

- Row is **preserved** and exported as-is
- `Email` and `Email_All` remain blank
- Attribution columns record `genuine_no_email` or `unknown_or_indeterminate`
- Artist is **not** considered unusable

### Is "no email = unusable artist" assumed?

**No.** The codebase explicitly preserves rows without emails. However, some export filters (e.g., Woodpecker preset `has_primary_email`) will exclude no-email rows at the final edge.

### Duplicated email-resolution logic

| Logic | Locations |
|-------|-----------|
| Regex email extraction from HTML | `website_email_scraper.py`, `night_mode_fb.py`, `cross_directory_enricher.py`, `scripts/ig_email_probe.py` |
| Obfuscated email normalization | `email_normalizer.py`, `night_mode_fb.py` |
| Email deduplication / merging | `pipeline_runner._set_email_all`, `cross_directory_enricher._merge_email_all`, `email_provenance.merge_email_provenance_into_target` |
| `mailto:` detection | `website_email_scraper.py`, `night_mode_fb.py`, `cross_directory_enricher.py` |
| FB candidate override | `fb_email_override.py`, `cross_directory_enricher.py` (corporate-token lists overlap with `facebook_enrich.py`) |
| Skip-gate duplication | `fb_email_skip_gate.py`, `cross_directory_enricher.py::_row_has_email`, `cross_directory_enricher.py::_row_allows_heavy_enricher` |

---

## 10. Validation

### Validation stages

| Stage | File | What it checks |
|-------|------|----------------|
| **Pre-validate dedup** | `origin_validator.py:455` | Composite-key dedup before origin checks |
| **Origin HTML validation** | `origin_validator.py:630` | Fetches source pages, fuzzy-compares artist/track text |
| **Final checker** | `final_checker.py:428` | Genre cleanup, name consistency, directory conflict, duplicate flags, genre outlier, match score, final status |
| **Post-enrichment recompute** | `pipeline_runner.py:1578` | Repairs stale BLOCKs after emails added |
| **Export guard** | `pipeline_runner.py:3164` | Final `Needs_Review` computation |

### Expensive steps

- **Origin HTML fetching** — HIGH. One HTTP request (with Playwright fallback) per unique source URL, serial loop.
- **Facebook enrichment** — VERY HIGH. Selenium/Playwright, multiple navigations per row.
- **Fuzzy name matching** — MEDIUM. `fuzz.ratio` across up to 4 extracted names per row.

### Unnecessary blockers

1. **`BLOCKED_BY_ORIGIN` for rows with valid emails** — origin mismatch can downgrade email-holding rows to `BLOCKED_BY_ORIGIN` (partially softened to `REVIEW`).
2. **`directory_conflict_flag` false positives** — URL slug mismatches (e.g., different handles per platform) trigger `BLOCK`.
3. **Staleness downgrade** — releases before 2023 auto-downgraded from `OK` → `WARN` (default on, env-overridable).
4. **Genre outlier `WARN`** — genres in <5% of dataset flag the row.

### Recommendations for V1

| Step | Recommendation |
|------|----------------|
| Origin auto-validate | **Async / batch** — network-bound, advisory |
| Facebook enrichment | **Manual / supervised** — captcha interrupts, account risk |
| Staleness downgrade | **GUI toggle** — env var exists but is hidden |
| Genre outlier | **Optional** — adds review noise |
| Export guard | **Keep as-is** — cheap, conservative final check |

### Validation performed more than once?

**Yes — 4–5 times across the pipeline.** Each stage has new information, but a row can oscillate between `REVIEW`, `BLOCK`, and `OK`. The post-enrichment recompute is the only stage that can **repair** a `BLOCK`.

---

## 11. Export / Campaign Contract

### Final-output paths

| Path | Produced By | Format |
|------|-------------|--------|
| `master_export_leads.csv` | `pipeline_runner.export_master_leads` | Campaign-ready filtered CSV |
| Woodpecker preset | `lead_vault/exporter.py` | 21-column CSV |
| Final export preset | `lead_vault/exporter.py` | 30-column CSV with FB/IG state |
| Campaign Prep outputs | `CampaignPrepTab` | Recency-bucketed CSVs |

### Campaign schema (actual)

From `FINAL_EXPORT_COLUMNS` (`pipeline_runner.py:2819`) and `_build_final_export_frame` (`pipeline_runner.py:3199`):

- Identity: `Artist Name`, `Location`, `Country_Derived`, `Song Title`, `Primary Genre`, `Unearthed_Genre_Raw`
- Links: `Social Link`, `SoundCloud Link`, `Spotify_URL`, `External Links`
- Email: `Primary Email`, `All Emails`, `Email Source`, `Email_Source_URL`, `Email_Source_Type`, `Email_Extract_Method`
- Provenance: `Discovery Source`, `Lead_Source`, `Source_Directory`, `Source Directory`, `Source URL`, `Review_Urls`
- Signals: `Played on triple J`, `Played on Unearthed`, `Release Date`, `Date Added`
- Status: `final_status`, `Needs_Review`, `FB_Review_Reason`
- FB/IG state: `FB_Opportunity_State`, `FB_Gate_State`, `FB_Attempt_State`, `FB_Extract_State`, `FB_Write_State`, `FB_Debug_Reason`, `FB_Terminal_Reason`, `FB_Normalized_Terminal_Outcome`, `FB_Normalized_Terminal_Reason`, plus equivalent IG columns

### Source provenance through export

- `Lead_Source`, `Source_Directory`, `Discovery_Source`, `Source_URL` survive.
- `Email_Provenance_JSON` is **flattened** into 3 string columns. Rich JSON dropped.
- Different runtime paths emit **different schemas**:
  - Woodpecker preset: 21 headers
  - Final export preset: 30 headers
  - `export_master_leads`: uses `DEFAULT_EXPORT_COLUMNS`

### Issues

- **Multiple exporters duplicate transformations** — origin repair, status recompute, email consolidation exist in both bridge and `export_master_leads`.
- **Campaign copy fields mixed into discovery data** — FB/IG attribution columns (20+ fields) are present in the campaign schema even though they are internal operational metadata.
- **Old CSV compatibility shapes architecture** — dual column names (`Source_Directory` vs `Source Directory`, `Artist Name` vs `Artist`), `_backfill_column()`, encoding workarounds, `build_canonical_row()` with first-non-empty-wins.

### Recommended canonical export contract for V1

A single schema with three tiers:
1. **Identity & Contact** — artist name, location, genre, song, emails, social URLs, website
2. **Provenance** — source directory, source URL, discovery source, date added, email source URL/type/method
3. **Quality** — final status, needs review, review reason

Drop the 20+ FB/IG operational state machine columns from campaign exports. Keep them in the operational master CSV only.

---

## 12. Operator Workflow

### What a human operator must know

- **Hardcoded secrets live in `run_lead_machine.sh`** — Last.fm API key, Spotify client ID/secret/refresh token, SoundCloud client ID. These expire and must be rotated manually.
- **Virtual environment path is hardcoded** to a sibling directory (`…/Lead Machine Code/venv`). Won't launch on fresh machine.
- **Environment variables control deep behavior** — `ENRICHMENT_MODE`, `FB_*`, `NIGHT_*`, `SC_*`, `EMAIL_ALL_*` flags set in shell script; operators not exposed in GUI.
- **Facebook credentials entered per-session in GUI** — not persisted securely.
- **Master CSV path is implicit** — defaults to `data/master_lead_machine_contacts.csv`.

### Main GUI tabs

| Tab | Purpose |
|-----|---------|
| **Artist Scraping** | Scrape artist lists from sources |
| **Facebook Scraping** | Enrich CSV with FB emails |
| **Cross-Directory Enricher** | Merge seed CSV with directory CSVs + live search |
| **Lead Vault** | Import, merge, export master dataset |
| **Campaign Prep** | Generate campaign CSVs from master |
| **Auto-Validate** | Run origin validation on a CSV |
| **Night Mode** | Configure and launch overnight batch jobs |

### Confusing states / hidden assumptions

- **Cross-Directory Enricher silently disables** if `cross_directory_enricher.py` is missing — greyed-out button with no explanation.
- **Night Mode "Auto" resume mode** is ambiguous — does not explain checkpoint vs cursor precedence.
- **Auto-Validate writes to implicit path** (`*_origin.csv`) with no save-dialog.
- **Lead Vault master selector** may show files that do not exist — globs `*.csv` and inserts default even if absent.
- **Multiple overlapping FB recovery buttons** — ManualFbRecoveryWorker, ManualFbShareRecoveryWorker, FbDriverRecoveryWorker, Night Mode recovery section. Operator cannot easily tell which to use.

### Issue categorization

| Severity | Issue |
|----------|-------|
| **Launch Blocker** | `run_lead_machine.sh` hardcodes absolute paths and live API secrets |
| **Launch Blocker** | `cross_directory_enricher.py` soft-dependency disables core tab without clear UI message |
| **Annoying but acceptable** | Facebook captcha requires manual babysitting during Night Mode |
| **Annoying but acceptable** | Auto-Validate implicit output path |
| **Annoying but acceptable** | Lead Vault header mapping is manual per-import |
| **Annoying but acceptable** | Three separate FB recovery workflows |
| **Polish only** | Log consoles do not persist across restarts |
| **Polish only** | Campaign Prep does not preview recency bucket splits |

### Typical campaign workflow

1. **Seed / Discovery** — Artist Scraping tab or import into Lead Vault
2. **Enrichment** — Cross-Directory Enricher or Night Mode
3. **Facebook & Contact** — Facebook Scraping tab; manual recovery if Night Mode FB fails
4. **Origin Validation** — Auto-Validate tab
5. **Final Status & Review** — implicit final_checker + manual inspection of BLOCK/WARN rows
6. **Campaign Generation** — Campaign Prep tab (choose format, split emails, recency buckets)
7. **Export / Delivery** — Lead Vault export presets or Campaign Prep outputs

---

## 13. Security / Operational Risks

### Critical risks

| Risk | Location | Severity | Remediation |
|------|----------|----------|-------------|
| **Hardcoded Last.fm API key** | `run_lead_machine.sh:17` | CRITICAL | Rotate key. Move to `.env` file that is gitignored. |
| **Hardcoded Spotify Client ID/Secret** | `run_lead_machine.sh:20-21`, `spotify_oauth_setup.py:19-20` | CRITICAL | Rotate credentials. Remove from source. |
| **Hardcoded Spotify Refresh Token** | `run_lead_machine.sh:23` | CRITICAL | Rotate token. This grants persistent API access. |
| **Hardcoded SoundCloud Client ID** | `run_lead_machine.sh:108` | CRITICAL | Rotate key. Move to `.env`. |
| **Committed Chrome browser profile** | `.../` directory | CRITICAL | `git rm -r --cached "..."`. Contains cookies, login data, session tokens, trust tokens. |
| **Committed PII in CSVs/logs** | `data/`, `overnight_runs/`, `*.log` files | CRITICAL | Strip from git history (BFG or `git-filter-repo`). Add to `.gitignore`. |

### High risks

| Risk | Location | Severity | Remediation |
|------|----------|----------|-------------|
| **Inadequate `.gitignore`** | `.gitignore` | HIGH | Add `.env*`, `*.log`, `*.csv`, `overnight_runs/`, `data/`, Chrome profiles, `.DS_Store`. |
| **Facebook scraping breakage** | `night_mode_fb.py`, `facebook_enrich.py` | HIGH | Any FB markup change breaks Selenium selectors. Have fallback plan. |
| **Account bans / ToS violations** | FB scraping modules | HIGH | Scraping emails from FB pages violates Meta ToS. Risk of account ban. |
| **Spotify unconfigured playlists** | `spotify_scraper.py:28-33` | MEDIUM | `TODO_FRESH_FINDS_*_PLAYLIST_ID` placeholders. Scraper silently skips if not overridden. |
| **SoundCloud hardcoded client ID** | `soundcloud_engine.py` | MEDIUM | Single hardcoded value; if revoked, all SC enrichment stops. |
| **Rate-limit fragility** | Multiple modules | MEDIUM | Cooldown logic scattered with inconsistent retry strategies. |
| **Log leakage of emails** | `nm_debug.log`, `overnight_runs/*/log.txt` | MEDIUM | Extracted emails logged at INFO level. |

---

## 14. Test & Confidence Assessment

### Test inventory

The repository contains **130+ test files** covering:

- **Atomic CSV writer** (`test_atomic_csv_writer.py`) — 8 tests
- **Bandcamp live lookup** (`test_bandcamp_live_lookup.py`) — 14 tests
- **Campaign prep** (`test_campaign_prep.py`) — 25 tests
- **Domain email reuse / org sidecar** (`test_domain_email_reuse_roles.py`, `test_domain_org_sidecar.py`, `test_domain_profile_metadata.py`) — 20+ tests
- **Email helpers / normalizer** (`test_email_all_helpers.py`) — comprehensive
- **Facebook enrichment** (`test_facebook_enrich_roles.py`, `test_fb_enrich_existing_url_without_email.py`, `test_fb_pass_cap.py`, `test_fb_guard.py`, `test_fb_candidate_ranking.py`, `test_fb_confidence_review.py`, `test_fb_no_safe_match.py`, `test_fb_url_handoff.py`, `test_fb_dom_gate_wait.py`, `test_fb_driver_recycle.py`, `test_fb_search_client_fallback.py`, `test_fb_email_override.py`, `test_fb_min_quality_gate.py`, `test_fb_category_extraction.py`, `test_fb_homepage_fallback_wait.py`) — 50+ tests
- **Final checker** (`test_final_checker_safe_lower.py`) — regression tests
- **Instagram email** (`test_instagram_email_enrich.py`, `test_ig_email_probe.py`) — 30+ tests
- **Lead Vault** (`test_lead_vault_importer.py`, `test_lead_vault_exporter.py`, `test_lead_vault_merge.py`, `test_lead_vault_gui.py`) — 40+ tests
- **Night Mode** (`test_night_mode_fb_status_normalization.py`, `test_night_mode_fb_attribution.py`, `test_night_mode_fb_helpers.py`, `test_night_mode_fb_explicit_session.py`, `test_night_mode_fb_evidence_debug.py`, `test_night_mode_fb_main_page_email_scan.py`, `test_night_mode_fb_search_fallback.py`, `test_night_mode_runner_dummy.py`, `test_night_runtime_chunk_reset.py`, `test_facebook_global_pass_nightmode.py`, `test_fb_global_pass_preserves_rows.py`) — 80+ tests
- **Origin validator** (`test_origin_validator_regression.py`) — regression tests
- **Pipeline enrichment** (`test_pipeline_enrichment_samefile.py`, `test_master_enrichment_failure_preserves_output.py`, `test_post_enrichment_final_status.py`) — 10+ tests
- **Progress state** (`test_progress_state.py`) — state machine tests
- **SoundCloud** (`test_soundcloud_aggregator.py`, `test_soundcloud_engine_switch.py`, `test_sc_rss_only_logic.py`) — 15+ tests
- **Spotify** (`test_spotify_seed_context_ingest.py`, `test_spotify_seed_enrichment_flow.py`, `test_spotify_zero_row_fallback.py`) — 10+ tests
- **Source scheduler** (`test_source_scheduler.py`) — 30+ tests
- **Website email** (`test_website_email_enrich.py`) — 10+ tests

### Safe tests run

Command: `python3 -m pytest tests/ -v --tb=line`  
Results: **1,524 passed, 40 failed, 3 warnings** (runtime ~9m42s)

#### Failure breakdown by area

| Area | Failed Tests | Likely Cause |
|------|-------------|--------------|
| **Instagram email enrichment** | 15 | One-hop bio-link extraction assertions mismatched; likely fixture/HTML structure drift or mock setup issue |
| **Night Mode FB helpers** | 10 | Chrome driver session creation failures (`SessionNotCreatedException`), `StopIteration` in mocked page sources, stalltrace perf_counter assertion mismatches |
| **Night Mode FB explicit session** | 3 | Driver error: persistent FB profile could not be used; assertions on email extraction with empty results |
| **Night Mode FB evidence/debug** | 3 | JSON evidence debug output mismatches; visible contact email rescue seam failures |
| **Night Mode FB attribution** | 1 | FB attribution column preservation mismatch |
| **Night Mode FB search fallback** | 1 | Terminal result code mismatch (`no_candidates` vs `pass_a_skipped_no_fb_url`) |
| **Night Mode FB main page scan** | 1 | Secondary email fetch assertion mismatch |
| **Campaign prep** | 2 | Processed master / sorted split release date buffer assertions |
| **Lead Vault schema** | 2 | Canonical schema order stability and auto-create header exactness |
| **Spotify seed enrichment** | 1 | Bandcamp terminal preservation mismatch |
| **FB enrich existing URL** | 1 | Disallowed resolved share URL stays skipped for unearthed |
| **Final export review policy** | 1 | Ranked primary email preference over weaker existing email |
| **Unearthed smoke runner** | 1 | Smoke config preservation / cap mismatch (`assert 15 == 3`) |

#### Pass/fail summary

- **Core discovery & vault**: Mostly passing. Lead Vault import/merge/export, atomic CSV writer, campaign prep (majority), source scheduler, SoundCloud engine, origin validator, and progress state tests pass.
- **Enrichment layers**: Mixed. Website email, SoundCloud RSS, and Bandcamp live lookup tests pass. Instagram and Night Mode Facebook tests have significant failures (26 of 40 failures).
- **Night Mode orchestration**: Runner dummy tests and FB global pass preservation tests pass. Failure concentrated in FB helper/explicit session/evidence modules.

**What the tests prove:**
- Unit-level behavior of individual functions (email normalization, CSV writing, merge logic, source scheduling)
- Fixture-based mock tests for SoundCloud RSS logic, Bandcamp live lookup, campaign prep formatting
- Regression tests for known past bugs (email smear, origin validator mismatches)
- Night Mode orchestration state machine (job resume, checkpoint logic)

**What the tests do NOT prove:**
- Scrapers work against live websites (selectors may have changed since test fixtures were captured)
- Facebook login and page navigation works in production (tests mock driver interactions; actual driver tests fail with `SessionNotCreatedException`)
- Instagram HTML structure is still valid (tests fail with assertion mismatches on bio-link extraction)
- SoundCloud client ID is still valid
- Spotify API credentials are still valid
- Rate limits and anti-bot defenses behave as expected
- Night Mode full end-to-end run succeeds on real data
- The 40 failures do not indicate whether the corresponding production code is broken against live sites; they indicate that test fixtures/mocks no longer match the current implementation

---

## 15. Legacy / Dead / Duplicate Architecture

### Code categorization

| File / Module | Category | Rationale |
|---------------|----------|-----------|
| `Lead Machine (Final Update 5).py` | **LEGACY BUT STILL REQUIRED** | 16K-line monolith still dynamically imported by `pipeline_runner` and tests for core GUI, export, and scraping logic. |
| `cross_directory_enricher.py` | **CURRENT BUT OVERCOMPLEX** | 21K-line mega-module doing directory matching, live search, website email, IG, FB, festival expansion, domain sidecars. Needs decomposition. |
| `night_mode_fb.py` | **CURRENT BUT OVERCOMPLEX** | 11K-line FB enricher with legacy helper remnants and heavy env-var sprawl. |
| `pipeline_runner.py` | **CURRENT BUT OVERCOMPLEX** | Bridges legacy monolith and new modules; contains 5+ duplicated helpers. |
| `facebook_enrich.py` | **LIKELY DEAD** | Superseded by `night_mode_fb.py`; only referenced in tests and recovery scripts. |
| `night_mode_v2/` | **CURRENT** | Active entrypoint for Night Mode. v2 `run_phased_night_mode` is called from `night_mode_runner.py main()`. |
| `discover.js` | **LIKELY DEAD** | Committed scraper artifact; no Python imports. |
| `spotify_oauth_setup.py` | **CURRENT BUT FRAGILE** | Hardcodes credentials; prints refresh token to stdout. |
| `.../` (Chrome profile) | **DEAD / DANGEROUS ARTIFACT** | Committed browser profile with cookies and session data. |
| `data/`, `overnight_runs/`, `*.log` | **DANGEROUS ARTIFACTS** | Committed PII and runtime outputs. |

### Duplicate representations

| Concept | Locations | Status |
|---------|-----------|--------|
| Facebook URL canonicalization | `source_scheduler.py`, `facebook_enrich.py`, `night_mode_fb.py`, `fb_attribution.py`, `scripts/recover_fb_share_rows.py` | CURRENT BUT OVERCOMPLEX — 4+ implementations |
| Email normalization | `email_normalizer.py`, `fb_email_skip_gate.py`, `cross_directory_enricher.py` | CURRENT — some overlap |
| FB candidate ranking | `facebook_enrich.py`, `night_mode_fb.py`, `facebook_scoring_test.py` | LEGACY + CURRENT DUPLICATION |
| Spotify token management | `spotify_client.py`, `Lead Machine (Final Update 5).py` | DUPLICATE |
| SoundCloud engine | `soundcloud_engine.py`, `cross_directory_enricher.py` | CURRENT BUT OVERCOMPLEX |
| Email consolidation | `pipeline_runner.py`, `cross_directory_enricher.py`, `lead_vault/merge.py` | DUPLICATED |
| Final status recompute | `pipeline_runner.py`, `lead_vault/exporter.py` | DUPLICATED |
| Origin integrity repair | `pipeline_runner.py`, `lead_vault/origin.py`, `lead_vault/exporter.py` | DUPLICATED |

### Historical layers found

- `_load_legacy_module()` — called in 15+ test files and 4 production scripts
- `_legacy_fb_*` functions inside `Lead Machine (Final Update 5).py`
- `_scrape_fb_page_unearthed_legacy` in `night_mode_fb.py:6081`
- `TODO_FRESH_FINDS_*_PLAYLIST_ID` placeholders in `spotify_scraper.py`
- Commented-out driver test code in `night_mode_fb.py:3476-3483`
- `tests/test_fb_deprecated_helpers.py` — confirms deprecated helpers still exist

### Environment variable sprawl

**60+ environment variables** control behavior:
- `FB_DEBUG_*` (DOM gate, candidates, ranking, music signals, email override)
- `NIGHT_FB_*` (checkpoint guard, min quality, profile dir, DOM fallback)
- `SC_*` / `NIGHT_SC_*` (cooldowns, RSS thresholds, engine selection)
- `BC_DEBUG_*` (Bandcamp debug)
- `IG_TRUTH_CAPTURE`, `IG_TRUTH_TARGET_URL`

This indicates the system is being tuned via env vars rather than configuration files, making runtime behavior unpredictable.

---

## 16. Simplification Opportunities

### Opportunity 1: Unify normal and Night Mode processing

**Current:** Daytime GUI calls scrapers directly. Night Mode calls them through `pipeline_runner`. Dataframe guards (quarantine, smear guard, dedupe) only exist in Night Mode.

**Why it exists:** Night Mode was added later as an orchestration layer without refactoring the GUI paths.

**Current cost:** Duplicated scraper invocation logic. Night Mode has better data quality guards that normal mode lacks.

**Simpler V1 alternative:** Make `pipeline_runner.run_directory_job` and `run_enrichment` the **only** paths for both GUI and Night Mode. GUI tabs should call `pipeline_runner` instead of calling scrapers directly.

**Migration difficulty:** Medium — requires GUI thread refactoring.

**V1 priority:** DO AFTER V1

---

### Opportunity 2: Separate artist discovery from email enrichment

**Current:** `spotify_scraper.py` mixes playlist discovery with About-page enrichment and website email extraction. `cross_directory_enricher.py` mixes directory matching, live search, website email, IG, and FB.

**Why it exists:** Historical growth — each new enrichment was added to the existing orchestrator.

**Current cost:** 21K-line god module. Hard to test, hard to reason about, hard to skip a failing enrichment stage.

**Simpler V1 alternative:** Discovery produces a "candidate" CSV. Enrichment is a separate pipeline that reads the candidate CSV and adds contact columns. Each enrichment stage (website, IG, FB) is an independent transform that can be run or skipped.

**Migration difficulty:** High — touches `cross_directory_enricher.py`, `pipeline_runner.py`, GUI tabs.

**V1 priority:** DO AFTER V1

---

### Opportunity 3: One canonical lead schema

**Current:** 83-field `CANONICAL_MASTER_SCHEMA` in `lead_vault/schema.py`. Dual column names (`Source_Directory` vs `Source Directory`, `Artist Name` vs `Artist`). 20+ FB/IG operational columns mixed into campaign schema.

**Why it exists:** CSV fragility — columns were added incrementally, old exports needed backward compatibility.

**Current cost:** `_backfill_column()`, `repair_origin_fields()`, `_build_legacy_final_export_bridge_frame` exist solely to paper over column drift.

**Simpler V1 alternative:** Define one 30-field canonical schema. Drop the legacy aliases. Keep operational FB/IG columns in a separate "enrichment audit" CSV, not in the campaign export.

**Migration difficulty:** Medium — requires updating all exporters and the GUI.

**V1 priority:** DO AFTER V1

---

### Opportunity 4: One canonical exporter

**Current:** Three export paths with overlapping logic: Woodpecker preset, final export preset/legacy bridge, `export_master_leads`.

**Why it exists:** Different downstream tools (Woodpecker, Lead Machine GUI, manual review) needed different columns.

**Current cost:** `repair_origin_integrity_df`, `recompute_final_status_post_enrichment`, email consolidation duplicated across paths.

**Simpler V1 alternative:** One `export_master_leads` function that reads the canonical schema and projects subsets. No legacy bridge.

**Migration difficulty:** Low — mostly deletion.

**V1 priority:** DO NOW (low effort, immediate clarity)

---

### Opportunity 5: Simpler provenance model

**Current:** Row-level origin fields + email-level JSON provenance + origin validator scores + FB/IG attribution columns. Provenance is lossily compressed at export.

**Why it exists:** Attempt to track every decision without a database.

**Current cost:** `email_provenance.py` is sophisticated but the JSON is dropped at export. `Email_Provenance_JSON` column adds noise to DataFrames.

**Simpler V1 alternative:** Keep `Email_Source_URL`, `Email_Source_Type`, `Email_Extract_Method` as first-class columns. Drop the JSON blob. Track row-level origin in `Lead_Source`, `Source_Directory`, `Source_URL` only.

**Migration difficulty:** Low.

**V1 priority:** DO NOW

---

### Opportunity 6: Replace multiple dedupe passes with one identity layer

**Current:** Deduplication happens in `origin_validator.dedupe_pre_auto_validate`, `final_checker.run_final_checker`, `lead_vault/merge.py` (standard + consolidating), `night_mode_runner._merge_raw_master`, and `pipeline_runner.export_master_leads`.

**Why it exists:** Each stage added its own dedupe as new failure modes were discovered.

**Current cost:** Hard to know which dedupe "won". Source provenance can be lost during merges.

**Simpler V1 alternative:** One dedupe step at ingestion into Lead Vault. Use a simple deterministic rule: `(Source_URL)` primary key, `Artist + Location` fallback. Everything else is a merge, not a dedupe.

**Migration difficulty:** Medium — requires refactoring `lead_vault/merge.py` and removing dedupe from upstream stages.

**V1 priority:** DO AFTER V1

---

### Opportunity 7: Turn complex automation into explicit operator-assisted steps

**Current:** Facebook enrichment tries to be fully automated but requires captcha babysitting, checkpoint recovery, and manual driver recovery.

**Why it exists:** Desire to run unattended overnight.

**Current cost:** 11K lines of `night_mode_fb.py` for a task that still needs human intervention.

**Simpler V1 alternative:** Treat FB enrichment as a **supervised batch job**. Operator logs in, starts the pass, intervenes on captcha, reviews results. The code can be 80% smaller if it does not need unattended resume, auto-recovery, and trust budgets.

**Migration difficulty:** High — would require rewriting `night_mode_fb.py`.

**V1 priority:** DO AFTER V1 (but accept manual FB operation for V1)

---

### Opportunity 8: Remove old compatibility layers

**Current:** `Source Directory` / `Source_Directory` duality, `Artist Name` / `Artist` duality, `_backfill_column()`, `repair_origin_fields()`, `_build_legacy_final_export_bridge_frame()`.

**Why it exists:** Old CSVs had different column names.

**Current cost:** Every import, export, and merge pays the complexity tax.

**Simpler V1 alternative:** Migrate all historical CSVs to the canonical schema once. Delete all compatibility code.

**Migration difficulty:** Low — one-time data migration + deletion.

**V1 priority:** DO NOW

---

### Opportunity 9: Remove speculative abstractions

**Current:** `night_mode_v2/merge_rules.py` defines declarative merge rules (`fill_blank`, `union_multi`, `status_worst_wins`, `max_wins`) but they are **not used** by the active v1 merge paths in `night_mode_runner.py`.

**Why it exists:** v2 intended to replace imperative merge logic but never fully wired in.

**Current cost:** Dead code that looks active.

**Simpler V1 alternative:** Either wire v2 merge rules into the pipeline or delete them.

**Migration difficulty:** Low.

**V1 priority:** DO NOW

---

### Opportunity 10: Stop persisting fields that are never used

**Current:** `CANONICAL_MASTER_SCHEMA` has 83 fields. Many are never referenced in export or campaign generation:
- `Career_Stage`, `Industry_Signals`, `Played_On_Community_Radio`
- 20+ FB/IG operational state columns in campaign exports
- `Email_Provenance_JSON` (dropped at export)

**Why it exists:** Schema designed for a fuller product vision.

**Current cost:** Wider DataFrames, more merge logic, more confusion.

**Simpler V1 alternative:** Drop unused fields from the master schema. Add them back only when a feature actually uses them.

**Migration difficulty:** Low.

**V1 priority:** DO NOW

---

## 17. V1 Launch Blockers

These are things that genuinely make V1 unsafe or unreliable:

1. **Hardcoded API secrets in version control** (`run_lead_machine.sh`, `spotify_oauth_setup.py`). Risk: key rotation on any breach, credentials visible to anyone with repo access.
2. **Committed Chrome browser profile** (`.../`). Risk: live session cookies, possible cached Facebook credentials, device fingerprint exposure.
3. **Committed PII in CSVs and logs** (`data/`, `overnight_runs/`, `*.log`). Risk: real artist emails, names, locations in git history. GDPR/privacy liability.
4. **Inadequate `.gitignore`** — missing `.env*`, `*.log`, `*.csv`, `overnight_runs/`, `data/`, Chrome profiles.
5. **Spotify playlist placeholders** (`TODO_FRESH_FINDS_*_PLAYLIST_ID` in `spotify_scraper.py:28-33`). Risk: Spotify discovery silently produces zero rows if not configured.
6. **`cross_directory_enricher.py` soft-dependency disables core tab** — GUI greyed out with no clear message if file missing.
7. **`run_lead_machine.sh` hardcoded absolute paths** — virtual env path, Chrome driver path. Won't run on any machine except the developer's.

---

## 18. Manual-Assistance Procedures

These are things a human can safely handle for V1:

1. **Facebook captcha / checkpoint intervention** — Operator logs in, handles FB security checks, restarts the pass.
2. **Spotify API credential rotation** — Manual `.env` update when tokens expire.
3. **SoundCloud client ID rotation** — Manual update when hardcoded ID is revoked.
4. **Bandcamp / Unearthed selector drift** — Operator updates selectors when sites change.
5. **Origin validation review** — Operator inspects `BLOCKED_BY_ORIGIN` rows and manually overrides where appropriate.
6. **Final status review** — Operator inspects `BLOCK` / `WARN` rows and `Review_Urls` before campaign generation.
7. **Night Mode resume / recovery** — Operator handles driver errors, `/share` link recovery, and re-runs failed jobs.
8. **Lead Vault header mapping** — Operator maps incoming CSV headers to canonical fields per-import.
9. **Campaign Prep export format selection** — Operator knows which preset the downstream tool expects.

---

## 19. Post-V1 Work

These should explicitly wait until after V1 is generating revenue:

1. **Architectural decomposition of `cross_directory_enricher.py`** — Split into discovery, enrichment, and export modules.
2. **Consolidation of `night_mode_fb.py`** — Promote session logic to shared module; thin out Night Mode-specific wrapper.
3. **Database migration** — Replace CSV-centric master with SQLite/JSONL/Parquet. Eliminate stringly-typed fields and `;`-delimited lists.
4. **Full test suite against live sources** — Periodic smoke tests against real websites to catch selector drift.
5. **Automated credential rotation** — OAuth refresh automation, secret manager integration.
6. **Self-service operator onboarding** — Eliminate hardcoded paths, provide setup wizard, document env vars in GUI.
7. **Source adapter plugin architecture** — Make new sources pluggable instead of adding to the monolith.
8. **Full removal of `facebook_enrich.py`** — Once all tests and recovery scripts migrate to `night_mode_fb.py`.
9. **Unification of normal and Night Mode paths** — GUI tabs should call `pipeline_runner` consistently.
10. **Replace 60+ env vars with typed config** — Single config file/TOML with validation.

---

## 20. Recommended Target V1 Architecture

Based on what the repository actually contains, the **minimal reliable V1 architecture** is:

```text
┌─────────────────────────────────────────────────────────────┐
│                     Operator GUI (PyQt5)                     │
│  Artist Scraping → Cross-Directory Enricher → Lead Vault    │
│  Night Mode (v2 phased) → Campaign Prep → Export            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Pipeline Runner                            │
│  run_directory_job → run_enrichment → run_master_enrichment │
│  run_facebook_global_pass_nightmode → export_master_leads   │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│   Discovery   │    │  Enrichment   │    │   Lead Vault  │
│   Adapters    │    │   (optional)  │    │               │
│               │    │               │    │  Master CSV   │
│  Spotify      │    │  Website      │    │  Deduplicate  │
│  Bandcamp     │    │  Instagram    │    │  Merge        │
│  SoundCloud   │    │  Facebook*    │    │  Export       │
│  Unearthed    │    │  Live Search  │    │               │
│  Last.fm      │    │               │    │               │
└───────────────┘    └───────────────┘    └───────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              Canonical Campaign Export                       │
│  30-field schema: Identity + Contact + Provenance + Quality │
│  Drop 20+ FB/IG operational columns from campaign CSV       │
└─────────────────────────────────────────────────────────────┘
```

\* Facebook enrichment is **optional** for V1. Manual assistance acceptable.

### What survives

- Night Mode v2 phased orchestration (`night_mode_v2/phased_runner.py`)
- `pipeline_runner.py` as the canonical wrapper
- Source adapters: Spotify, Bandcamp, SoundCloud, Unearthed, Last.fm
- Lead Vault master CSV, dedupe, and export
- Email provenance tracking (simplified to 3 columns)
- Final checker quality flags (simplified)

### What gets simplified

- **Export**: One canonical exporter. Delete legacy bridge.
- **Schema**: Drop unused fields and legacy column aliases.
- **Provenance**: Flatten `Email_Provenance_JSON` to first-class columns.
- **Validation**: Make origin validation async/manual. Disable staleness downgrade by default.

### What can be removed

- `facebook_enrich.py` (legacy daytime FB enricher)
- `discover.js` (committed artifact)
- `festival_scraper.py` (defer until selector stability improves)
- v1 monolithic `run_night_mode` (once v2 fully subsumes it)
- Unused `night_mode_v2/merge_rules.py` (if not wired in)

### What gets deferred

- Database migration (SQLite/JSONL)
- Full decomposition of `cross_directory_enricher.py`
- Self-service onboarding
- Plugin architecture
- Automated credential rotation

### What remains manual

- Facebook captcha/checkpoint handling
- API credential rotation
- Selector drift updates for Bandcamp/Unearthed
- Origin validation review for edge cases
- Final campaign review before export

---

## 21. Prioritized Action Plan

| Priority | Action | Why | Risk if skipped | Effort | V1 blocker? |
|----------|--------|-----|-----------------|--------|-------------|
| **P0** | Rotate all hardcoded API keys/tokens (Last.fm, Spotify, SoundCloud) | Secrets are in version control | Account compromise, unauthorized API access | Low | **YES** |
| **P0** | Purge committed Chrome profile (`.../`) from git history | Contains session cookies and possible cached credentials | Session hijacking, credential leak | Low | **YES** |
| **P0** | Purge committed PII (CSVs, logs) from git history | Real artist emails, names, locations in repo | GDPR/privacy liability, data breach | Medium | **YES** |
| **P0** | Fix `.gitignore` to exclude `.env*`, `*.log`, `*.csv`, `overnight_runs/`, `data/`, Chrome profiles | Prevents future accidental commits of secrets and PII | Recurrence of P0 issues | Low | **YES** |
| **P0** | Move secrets from `run_lead_machine.sh` to a sourced `.env.local` | Hardcoded secrets in shell script | Same as key rotation risk | Low | **YES** |
| **P1** | Configure Spotify `TODO_FRESH_FINDS_*_PLAYLIST_ID` placeholders | Scraper silently skips if not set | Zero Spotify output on runs | Low | **YES** |
| **P1** | Remove hardcoded defaults from `spotify_oauth_setup.py` | Duplicate credential exposure | Credential leak | Low | **YES** |
| **P1** | Add clear UI message when `cross_directory_enricher.py` is missing | Core tab silently disabled | Operator confusion, cannot run enrichment | Low | **YES** |
| **P1** | Fix `run_lead_machine.sh` hardcoded absolute paths | Won't run on fresh machine | Cannot launch application | Low | **YES** |
| **P1** | Disable staleness downgrade by default or add GUI toggle | Good leads downgraded purely for age | Missed opportunities | Low | No |
| **P1** | Consolidate to one canonical exporter (delete legacy bridge) | Three exporters with duplicated logic | Export inconsistencies, maintenance burden | Low | No |
| **P1** | Flatten `Email_Provenance_JSON` to first-class columns or drop it | JSON blob is lossily compressed at export anyway | Simpler schema, less DataFrame noise | Low | No |
| **P1** | Drop unused fields from `CANONICAL_MASTER_SCHEMA` | 83 fields, many never used | Wider DataFrames, merge confusion | Low | No |
| **P2** | Unify `Source_Directory` / `Source Directory` duality and other aliases | Old compatibility code shapes architecture | Continued complexity tax | Medium | No |
| **P2** | Wire v2 merge rules or delete `night_mode_v2/merge_rules.py` | Dead code that looks active | Maintenance confusion | Low | No |
| **P2** | Delete `facebook_enrich.py` and migrate remaining tests to `night_mode_fb.py` | Superseded module still present | False confidence in dead code | Medium | No |
| **P2** | Move dataframe guards (quarantine, smear guard) to shared module | Currently Night Mode only | Normal mode lacks data quality guards | Medium | No |
| **P3** | Decompose `cross_directory_enricher.py` into discovery/enrichment/export modules | 21K-line god module | Unmaintainable, high bug surface | High | No |
| **P3** | Consolidate `night_mode_fb.py` session logic into shared FB manager | 11K lines for one enrichment source | Unmaintainable | High | No |
| **P3** | Unify checkpoint systems under v2 manifest | Row-level + job-level + manifest-level coexist | Resume complexity | Medium | No |
| **P3** | Migrate master CSV to SQLite/JSONL | CSV-centric architecture limits reliability | Data integrity, concurrent access | High | No |
| **P3** | Replace 60+ env vars with typed config file | Unpredictable runtime behavior | Hard to reproduce issues | Medium | No |

---

## 22. Final V1 Readiness Verdict

### Verdict: **YES, AFTER SMALL FIXES**

Lead Machine can be used with real Studiflow customers next week if Hugh operates it manually and fixes exceptions as they arise.

### What makes this possible

- Core discovery paths (Spotify API, Bandcamp tag/search, SoundCloud v2 API, Unearthed indexed mode, Last.fm API) are functional.
- Night Mode v2 provides a solid unattended orchestration layer for seed → enrich → contact phases.
- Email enrichment has multiple fallback paths (website, Instagram, directory data) so Facebook outages do not block campaigns.
- Lead Vault deduplication and export produce usable campaign CSVs.
- The GUI gives the operator visibility and control over each stage.

### What must happen first (P0 / P1)

1. **Security hygiene** — rotate keys, purge committed Chrome profile and PII from git, fix `.gitignore`, move secrets to `.env`.
2. **Operational guardrails** — configure Spotify playlists, fix shell launcher paths, add missing-enricher UI message.
3. **Simplify exports and schema** — one canonical exporter, drop unused fields, flatten provenance.

### What is acceptable as manual assistance for V1

- Facebook captcha / checkpoint babysitting
- API credential rotation
- Selector drift updates
- Origin validation review
- Final campaign review

### What should wait until post-V1

- Major architectural decomposition
- Database migration
- Full test automation against live sites
- Self-service onboarding
- Plugin architecture

### Top 5 simplification opportunities

1. **One canonical exporter** — delete the legacy bridge (low effort, immediate clarity).
2. **Simplify schema** — drop the 20+ unused fields and legacy column aliases.
3. **Flatten provenance** — replace `Email_Provenance_JSON` with first-class columns or drop it.
4. **Remove dead modules** — `facebook_enrich.py`, `discover.js`, unused v2 merge rules.
5. **Unify normal and Night Mode paths** — make `pipeline_runner` the single entry point for both GUI and unattended runs.

### Items requiring separate live/runtime verification

- **Facebook scraping against live site** — DOM selectors in `night_mode_fb.py` may have drifted since last test fixture capture.
- **SoundCloud client ID validity** — hardcoded candidates may be expired; JS scraping fallback needs verification.
- **Bandcamp grid selectors** — 8 fallback selectors need verification against current Bandcamp HTML.
- **Unearthed hashed CSS selectors** — `HU3iy.p1_Ju.mqDRk.FQED6.O_grP` class names are brittle and may have changed.
- **Spotify about-page `__NEXT_DATA__` structure** — internal Next.js payload may have changed.
- **Instagram profile HTML structure** — IG frequently changes its markup; direct HTML regex may be stale.
- **Night Mode v2 end-to-end on real data** — manifest-driven flow should be tested with a real multi-source config.

---

*End of audit report.*
