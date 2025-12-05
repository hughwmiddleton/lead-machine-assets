## Unearthed Night Mode regression – findings

- Night Mode invokes Unearthed via `run_directory_job` in `pipeline_runner.py` (see directory == "unearthed"), which currently calls the simple `scrape_website` listing scraper. That scraper (in `Lead Machine (Final Update 5).py`, `scrape_website` / `scrape_artist_profile`) only collects socials + metadata; it never runs the “page-2 contact/email” pass, so Email/Email_All are never populated in the Unearthed CSV produced during Night Mode runs.
- The master merge in `night_mode_runner._merge_raw_master` / `_merge_master` concatenates per-job CSVs without coalescing email fields or preferring rows with an email, so even if a directory provided an email, later merges/dedupes could drop it in favour of an email-less duplicate.
- Result: Unearthed rows in `master_raw.csv` carry `Source Directory=job_unearthed_*` and socials/flags, but Email/Email_All stay empty because the full legacy Unearthed pipeline (with contact/email pass) is never invoked, and merge/dedupe does not protect email-bearing rows.

## Patch applied

- Night Mode now tries to run a full Unearthed pipeline (`run_unearthed_pipeline` if present), falling back to `scrape_website` only if the full entrypoint is unavailable. This aligns the Night Mode call-site with the legacy “page-2 contact” behaviour without altering scraper internals.
- Added email coalescing during master raw/enriched merges (including syncing Email_All from Email when missing) and tweaked dedupe ordering to keep rows with Email over those without, preventing email loss when collapsing duplicates or merging directories.
- Email writes during FB passes now guard against overwriting existing Email with blanks, preserving any directory-provided (e.g., Unearthed) emails.

Run note: `python3 -m py_compile pipeline_runner.py night_mode_runner.py` to sanity check; then run a Night Mode job that includes an Unearthed artist (e.g., zuso) and confirm the Unearthed CSV and master outputs retain the email found by the legacy contact pass.
