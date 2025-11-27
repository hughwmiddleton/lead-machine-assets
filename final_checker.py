import os
import re
from urllib.parse import urlparse, unquote

import pandas as pd
from rapidfuzz import fuzz
from unidecode import unidecode


def _clean_name(value: str) -> str:
    text = unidecode(str(value or "")).lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_spotify_id(slug: str) -> bool:
    condensed = re.sub(r"[^a-zA-Z0-9]", "", slug or "")
    return bool(re.fullmatch(r"[A-Za-z0-9]{16,}", condensed))


def _extract_url_from_row(row: pd.Series, keyword: str) -> str:
    for _, value in row.items():
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        lower = candidate.lower()
        if candidate.startswith(("http://", "https://")) and keyword in lower:
            return candidate
    return ""


def _extract_spotify_name(row: pd.Series) -> str:
    url = _extract_url_from_row(row, "spotify.com")
    if not url:
        return ""
    parsed = urlparse(url)
    parts = [p for p in (parsed.path or "").split("/") if p]
    slug = ""
    if "artist" in parts:
        idx = parts.index("artist")
        if idx + 1 < len(parts):
            slug = parts[idx + 1]
    if not slug and parts:
        slug = parts[-1]
    slug = unquote(slug.split("?", 1)[0])
    slug = slug.replace("-", " ").replace("_", " ").strip()
    if not slug or _looks_like_spotify_id(slug):
        return ""
    return _clean_name(slug)


def _extract_bandcamp_name(row: pd.Series) -> str:
    url = _extract_url_from_row(row, "bandcamp.com")
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    candidate = ""
    if host.endswith("bandcamp.com"):
        subdomain = host.split(".")[0]
        if subdomain not in ("www", "bandcamp", ""):
            candidate = subdomain
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not candidate and parts:
        candidate = parts[0]
    candidate = unquote(candidate)
    return _clean_name(candidate)


def _extract_soundcloud_name(row: pd.Series) -> str:
    url = _extract_url_from_row(row, "soundcloud.com")
    if not url:
        return ""
    parsed = urlparse(url)
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return ""
    candidate = unquote(parts[0])
    return _clean_name(candidate)


def _extract_facebook_name(row: pd.Series) -> str:
    url = _extract_url_from_row(row, "facebook.com")
    if not url:
        return ""
    parsed = urlparse(url)
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return ""
    if parts[0] in ("pages",):
        candidate = parts[1] if len(parts) > 1 else ""
    elif parts[0] == "profile.php":
        candidate = ""
    else:
        candidate = parts[0]
    candidate = unquote(candidate.replace("-", " ").replace("_", " "))
    return _clean_name(candidate)


def _pairwise_conflict(names: list[str], threshold: float) -> int:
    if len(names) < 2:
        return 0
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if fuzz.ratio(names[i], names[j]) < threshold:
                return 1
    return 0


def run_final_checker(final_csv_path: str) -> str:
    try:
        if not final_csv_path or not os.path.exists(final_csv_path):
            print(f"[Final Checker] Warning: file not found at {final_csv_path}")
            return final_csv_path

        df = pd.read_csv(final_csv_path)
        result_df = df.copy()
        total_rows = len(result_df)

        artist_series = result_df.get("Artist Name", pd.Series(dtype=str)).fillna("").astype(str)
        email_series = result_df.get("Email", pd.Series(dtype=str)).fillna("").astype(str)
        normalized_emails = email_series.str.strip().str.lower()
        normalized_artists = artist_series.apply(_clean_name)

        email_counts = normalized_emails[normalized_emails != ""].value_counts().to_dict()
        artist_counts = normalized_artists[normalized_artists != ""].value_counts().to_dict()

        genre_series = result_df.get("Primary Genre", pd.Series(dtype=str)).fillna("").astype(str)
        normalized_genres = genre_series.str.strip().str.lower()
        genre_counts = normalized_genres[normalized_genres != ""].value_counts().to_dict()

        name_flags = []
        dup_email_flags = []
        dup_artist_flags = []
        dir_conflict_flags = []
        genre_outlier_flags = []
        match_scores = []
        statuses = []

        for idx, row in result_df.iterrows():
            artist_clean = normalized_artists.iloc[idx]

            spotify_name = _extract_spotify_name(row)
            bandcamp_name = _extract_bandcamp_name(row)
            soundcloud_name = _extract_soundcloud_name(row)
            facebook_name = _extract_facebook_name(row)

            candidate_names = [n for n in (spotify_name, bandcamp_name, soundcloud_name, facebook_name) if n]
            if artist_clean and candidate_names:
                best_score = max(fuzz.ratio(artist_clean, candidate) for candidate in candidate_names)
                name_flag = 1 if best_score < 70 else 0
            else:
                name_flag = 0

            dir_names = [n for n in (bandcamp_name, soundcloud_name, facebook_name) if n]
            dir_conflict_flag = _pairwise_conflict(dir_names, 70)

            email_key = normalized_emails.iloc[idx]
            dup_email_flag = 1 if email_key and email_counts.get(email_key, 0) > 1 else 0

            artist_key = artist_clean
            dup_artist_flag = 1 if artist_key and artist_counts.get(artist_key, 0) > 1 else 0

            genre_key = normalized_genres.iloc[idx]
            if not genre_key or total_rows == 0:
                genre_flag = 0
            else:
                freq = genre_counts.get(genre_key, 0) / total_rows
                genre_flag = 1 if freq < 0.05 else 0

            match_score = 1.0 - 0.25 * (name_flag + dir_conflict_flag + dup_email_flag + dup_artist_flag)
            match_score = max(0.0, min(1.0, match_score))

            if name_flag == 1 or dir_conflict_flag == 1:
                status = "BLOCK"
            elif dup_email_flag == 1 or dup_artist_flag == 1 or genre_flag == 1:
                status = "WARN"
            else:
                status = "OK"

            name_flags.append(name_flag)
            dup_email_flags.append(dup_email_flag)
            dup_artist_flags.append(dup_artist_flag)
            dir_conflict_flags.append(dir_conflict_flag)
            genre_outlier_flags.append(genre_flag)
            match_scores.append(match_score)
            statuses.append(status)

        result_df["name_consistency_flag"] = name_flags
        result_df["duplicate_email_flag"] = dup_email_flags
        result_df["duplicate_artist_flag"] = dup_artist_flags
        result_df["directory_conflict_flag"] = dir_conflict_flags
        result_df["genre_outlier_flag"] = genre_outlier_flags
        result_df["match_score_overall"] = match_scores
        result_df["final_status"] = statuses

        base, ext = os.path.splitext(final_csv_path)
        checked_path = f"{base}_checked{ext or '.csv'}"
        result_df.to_csv(checked_path, index=False)
        return checked_path
    except Exception as exc:
        print(f"[Final Checker] Warning: checker failed safely: {exc}")
        return final_csv_path
