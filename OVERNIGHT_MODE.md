# Night / Overnight Mode

Night Mode is a thin orchestration layer that runs multiple Lead Machine directory scrapes in sequence, enriches each CSV, and optionally merges them into a master export. It keeps the existing scrapers untouched and adds a resumable, logged wrapper for unattended runs.

## Config file

Create a JSON config (see `overnight_jobs.example.json`) with:

- `export_mode`: `per_directory` | `combined` | `both` (default `both`).
- `jobs`: array of jobs with:
  - `job_id`: unique identifier for folder/state/log names.
  - `directory`: `spotify`, `bandcamp`, `soundcloud`, `lastfm` (additional dirs can be added to `pipeline_runner.py`).
  - `mode`: optional scraper mode (e.g. `discover`, `search`, `playlist`).
  - `input_seed_csv`: optional seed/config path (passed through to the scraper where applicable).
  - `target_valid_leads`: desired lead count to request from the scraper.
  - `max_hours`: optional per-job time cap.
  - `notes`: optional free text.

## Running

```bash
python night_mode_runner.py --config overnight_jobs.json
python night_mode_runner.py --config overnight_jobs.json --resume
python night_mode_runner.py --config overnight_jobs.json --export-mode combined
```

Flags:

- `--config PATH` (required) config JSON.
- `--resume` reuse the latest `overnight_runs/<timestamp>/` state and skip completed jobs.
- `--stop-on-failure` abort remaining jobs on the first failure.
- `--export-mode` override config export mode (`per_directory` / `combined` / `both`).
- `--run-root` root folder for runs (default `overnight_runs`).

## Outputs and state

- Runs live under `overnight_runs/<YYYY-MM-DD_HHMMSS>/`.
- Per job: `<run>/<job_id>/raw.csv`, `enriched.csv`, `log.txt`, `state.json`.
- State fields include `status` (`pending`, `running`, `partial_timeout`, `partial_error`, `failed`, `completed`), row counters, and timestamps.
- A config snapshot is stored in each new run directory.

## Resume

`--resume` finds the latest `overnight_runs` directory, skips jobs with `status=completed`, and reuses existing `raw.csv` files when present. Bandcamp checkpoints are kept in the job folder (`bandcamp_progress.json`).

## Master export

If `export_mode` is `combined` or `both`, Night Mode merges all per-job `enriched.csv` files into `master_enriched_deduped.csv` in the run root, removing duplicates by email or `(Artist Name + primary URL)`. Per-job enriched files are always kept for location/genre specific packs.

## Notes

- The orchestration layer calls existing scrapers via `pipeline_runner.py`; scraper logic is untouched.
- Enrichment uses `origin_validator.run_auto_validate` and `final_checker.run_final_checker` to add validation/duplicate flags while keeping existing behaviour.
- Logs go to stdout and per-job `log.txt` for post-run review.
