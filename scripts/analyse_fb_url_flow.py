import argparse
from pathlib import Path
from typing import Iterable, Set

import pandas as pd


def _load_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")


def _is_unearthed_row(row: pd.Series) -> bool:
    for key in ("Source Directory", "Source Tag", "__source_job"):
        val = str(row.get(key, "") or "").lower()
        if "unearthed" in val:
            return True
    return False


def _has_fb_clue(row: pd.Series) -> bool:
    for field in ("Facebook_URL", "Facebook URL", "Social Link", "External Links"):
        val = str(row.get(field, "") or "").lower()
        if "facebook.com" in val or "fb.com" in val:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare FB clues between master_raw and master_pre_fb.")
    parser.add_argument("--run-dir", required=True, help="Path to smoke run directory (contains master_raw.csv etc).")
    parser.add_argument(
        "--examples",
        type=int,
        default=10,
        help="Number of example artists to show where raw has FB but pre_fb does not.",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    raw_csv = run_dir / "master_raw.csv"
    pre_fb_csv = run_dir / "master_pre_fb.csv"

    if not raw_csv.exists() or not pre_fb_csv.exists():
        raise FileNotFoundError(f"Expected master_raw.csv and master_pre_fb.csv in {run_dir}")

    raw_df = _load_csv(raw_csv)
    pre_df = _load_csv(pre_fb_csv)

    raw_unearthed = raw_df[raw_df.apply(_is_unearthed_row, axis=1)]
    pre_unearthed = pre_df[pre_df.apply(_is_unearthed_row, axis=1)]

    raw_with_fb = raw_unearthed[raw_unearthed.apply(_has_fb_clue, axis=1)]
    pre_with_fb = pre_unearthed[pre_unearthed.apply(_has_fb_clue, axis=1)]

    raw_artists = set(raw_with_fb["Artist Name"].astype(str).str.strip().tolist())
    raw_artists.discard("")
    pre_artists = set(pre_with_fb["Artist Name"].astype(str).str.strip().tolist())
    pre_artists.discard("")
    missing_artists = sorted(list(raw_artists - pre_artists))[: args.examples]

    print("=== FB clue comparison (unearthed only) ===")
    print(f"run_dir: {run_dir}")
    print(f"raw rows: {len(raw_unearthed)}; with facebook.com/fb.com: {len(raw_with_fb)}")
    print(f"pre_fb rows: {len(pre_unearthed)}; with facebook.com/fb.com: {len(pre_with_fb)}")
    print(f"examples where RAW has FB but PRE_FB does not (n={len(missing_artists)} shown):")
    for name in missing_artists:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
