#!/usr/bin/env python3
"""
Lead Machine 11

This script scrapes artist data (Page 1) and Facebook pages (Page 2) and exports the results to CSV files.
It detects whether a song has been played on triple j or triple j unearthed by examining the drum logo on
the artist's page. The artist scraping output CSV includes two columns:
"Played on triple J" and "Played on Unearthed" (with "yes" if detected, or blank otherwise).
When scraping Facebook pages (Page 2), these columns are carried over from the input CSV.

Before running this script, please ensure you have installed the following packages:

    pip install pandas tqdm selenium beautifulsoup4 webdriver_manager PyQt5

Usage:
    python lead_machine11.py
"""

from __future__ import annotations

import sys
import subprocess
import platform

# ---------------------------
# Dependency Check and Installation
# ---------------------------
required_packages = {
    "pandas": "pandas",
    "tqdm": "tqdm",
    "selenium": "selenium",
    "bs4": "beautifulsoup4",
    "webdriver_manager": "webdriver_manager",
    "PyQt5": "PyQt5"
}

def install_package(package_name):
    """Install a package using pip."""
    print(f"Installing {package_name}...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])

def check_and_install_dependencies():
    missing_packages = []
    for import_name, package_name in required_packages.items():
        try:
            __import__(import_name)
        except ImportError:
            missing_packages.append((import_name, package_name))
    if missing_packages:
        print("The following packages are missing:")
        for imp, pkg in missing_packages:
            print(f" - {pkg}")
        ans = input("Would you like to install them now? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            for imp, pkg in missing_packages:
                try:
                    install_package(pkg)
                except Exception as e:
                    print(f"Failed to install {pkg}: {e}")
                    sys.exit(1)
        else:
            print("Dependencies are missing. Exiting.")
            sys.exit(1)

check_and_install_dependencies()

# ---------------------------
# Prevent Sleep on macOS using caffeinate
# ---------------------------
if platform.system() == "Darwin":
    print("Detected macOS – starting caffeinate to prevent sleep.")
    caffeinate_proc = subprocess.Popen(['caffeinate'])
else:
    caffeinate_proc = None

# ---------------------------
# Now import the dependencies
# ---------------------------
import os
import time
import random
import re
import pandas as pd
import datetime
from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse, urljoin
from PyQt5 import QtWidgets, QtCore

# ---------------------------
# Bandcamp Configuration
# ---------------------------
BANDCAMP_SEED_TAGS = ["united-kingdom", "london", "manchester", "brighton", "leeds", "bristol", "glasgow"]
BANDCAMP_PAGES_PER_TAG = 5
BANDCAMP_MIN_CONTACT_REQUIREMENT = True
BANDCAMP_DEFAULT_TAG_URL = "https://bandcamp.com/tag/united-kingdom"
UNEARTHED_DEFAULT_URL = "https://www.abc.net.au/triplejunearthed/music/"

# -----------------------------------------------------------------------------
# Helper: URL Normalization
# -----------------------------------------------------------------------------
def normalize_url(url):
    """Normalize a URL by stripping trailing slashes and converting to lowercase."""
    return url.rstrip('/').lower()

# -----------------------------------------------------------------------------
# Helper: Drum Status Detection from Page Source using BeautifulSoup
# -----------------------------------------------------------------------------
def get_drum_status_from_source(page_source):
    """
    Parses the page source to determine drum status for the most recent song release.
    
    It looks for the "Played on:" list (<ul> with class "oqAY3 PARBR") and then the first
    list item (<li data-component="ListItem"). Within that <li>:
      - If a screen-reader <span> (data-component="ScreenReaderOnly") is found and its text 
        contains "unearthed", returns "triple j unearthed".
      - Else if an SVG element with data-component "TripleJDrum" is found, returns "triple j".
      - Otherwise, returns an empty string.
    """
    soup = BeautifulSoup(page_source, 'html.parser')
    played_on_list = soup.find("ul", class_="oqAY3 PARBR")
    if played_on_list:
        li = played_on_list.find("li", attrs={"data-component": "ListItem"})
        if li:
            sr_span = li.find("span", attrs={"data-component": "ScreenReaderOnly"})
            if sr_span:
                text = sr_span.get_text().strip().lower()
                if "unearthed" in text:
                    return "triple j unearthed"
                elif "triple j" in text:
                    return "triple j"
            drum_svg = li.find("svg", attrs={"data-component": "TripleJDrum"})
            if drum_svg:
                return "triple j"
    return ""

# -----------------------------------------------------------------------------
# General Driver Setup for Artist Scraping (Headless)
# -----------------------------------------------------------------------------
def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

# -----------------------------------------------------------------------------
# Facebook Driver Setup (Visible / Head Mode with Optimizations)
# -----------------------------------------------------------------------------
def setup_facebook_driver():
    chrome_options = Options()
    # Run in visible mode for Facebook scraping.
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.page_load_strategy = 'eager'
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

# =============================================================================
# Scraping Functions for Artist Data (Page 1)
# =============================================================================
def scrape_website(url, existing_csv="artist_social_links.csv", max_artists=200):
    driver = setup_driver()
    artist_data = []
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'HU3iy'))
        )
        # Load existing CSV data if available
        existing_data = pd.DataFrame()
        if os.path.exists(existing_csv):
            existing_data = pd.read_csv(existing_csv)
        profile_urls = set()
        while len(profile_urls) < max_artists:
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            artist_links = soup.find_all('a', class_='HU3iy p1_Ju mqDRk FQED6 O_grP', href=True)
            for link in artist_links:
                href = link['href']
                if href.startswith('/triplejunearthed/artist/'):
                    profile_urls.add("https://www.abc.net.au" + href)
            print(f"Found {len(profile_urls)} artist profile URLs so far...")
            try:
                load_more_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, '//button[contains(text(), "Load more")]'))
                )
                load_more_button.click()
                time.sleep(random.uniform(3, 5))
            except Exception as e:
                print("No more 'Load More' button found or error:", e)
                break
        profile_urls = list(profile_urls)[:max_artists]
        print(f"Total artist profile URLs to scrape: {len(profile_urls)}")
        if not profile_urls:
            print("No artist profile URLs found. Please check the website structure or selectors.")
        for profile_url in profile_urls:
            social_links, location, song_title, sounds_like, artist_name, _ = scrape_artist_profile(driver, profile_url)
            # Determine drum status from the full page source.
            drum_status_raw = get_drum_status_from_source(driver.page_source)
            played_on_triplej = "yes" if drum_status_raw == "triple j" else ""
            played_on_unearthed = "yes" if drum_status_raw == "triple j unearthed" else ""
            artist_data.append((artist_name, location, song_title, sounds_like, social_links,
                                played_on_triplej, played_on_unearthed))
    except Exception as e:
        print(f"Error during website scraping: {e}")
    finally:
        driver.quit()
    save_to_csv(artist_data, existing_csv)

def scrape_artist_profile(driver, profile_url):
    social_links = []
    location = ""
    song_title = ""
    sounds_like = ""
    artist_name = profile_url.split('/')[-1]
    exclude_social_urls = {
        "https://www.facebook.com/triplejunearthed",
        "https://www.instagram.com/triple_j_unearthed",
        "https://twitter.com/triplejunearthd",
        "https://www.facebook.com/abc",
        "https://www.instagram.com/abcaustralia",
        "https://twitter.com/abcaustralia"
    }
    try:
        driver.get(profile_url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, 'body'))
        )
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        links = soup.find_all('a', href=True)
        for link in links:
            href = link['href']
            if any(domain in href for domain in ['facebook.com', 'instagram.com', 'twitter.com', 'spotify.com']):
                if normalize_url(href) in exclude_social_urls:
                    continue
                social_links.append(href)
        location_element = soup.find('div', class_='divwU')
        if location_element:
            location = location_element.get_text(strip=True)
        song_title_element = soup.find('span', class_='fRXHI')
        if song_title_element:
            song_title = song_title_element.get_text(strip=True)
        sounds_like_element = soup.find('h2', string="Sounds Like")
        if sounds_like_element:
            sounds_like_list = sounds_like_element.find_next('p')
            if sounds_like_list:
                sounds_like = sounds_like_list.get_text(strip=True)
    except Exception as e:
        print(f"Error scraping profile {profile_url}: {e}")
    return social_links, location, song_title, sounds_like, artist_name, ""

def save_to_csv(data, filename):
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")
    existing_data = pd.DataFrame()
    if os.path.exists(filename):
        existing_data = pd.read_csv(filename)
    new_data = []
    for artist_name, location, song_title, sounds_like, social_links, played_on_triplej, played_on_unearthed in data:
        for link in social_links:
            new_data.append({
                'Artist Name': artist_name,
                'Location': location,
                'Song Title': song_title,
                'Sounds Like': sounds_like,
                'Social Link': link,
                'Played on triple J': played_on_triplej,
                'Played on Unearthed': played_on_unearthed,
                'Date Added': current_date
            })
    combined_data = pd.concat([existing_data, pd.DataFrame(new_data)])
    combined_data = combined_data.drop_duplicates(subset=['Artist Name', 'Social Link'])
    combined_data.to_csv(filename, index=False)
    print(f"Data saved to {filename}")

# =========================== Bandcamp Scraper ===========================
def _bandcamp_extract_tag_from_url(url: str) -> str | None:
    if not url:
        return None
    match = re.search(r"/tag/([^/?#]+)", url)
    if match:
        return match.group(1).lower()
    return None

def scrape_bandcamp(seed_tags, pages_per_tag=5, existing_csv="artist_social_links.csv", max_artists=200):
    """High-level Bandcamp entry point. Iterates tags → paginated tag pages → collects candidate artist/album links → resolves to artist profile → extracts contacts → writes CSV."""
    driver = setup_driver()
    raw_candidates = []
    candidate_profiles = []
    seen_profiles = set()
    bandcamp_rows = []
    enriched_rows = []
    seen_artist_profiles = set()
    try:
        for tag in seed_tags:
            print(f"Bandcamp: scanning tag '{tag}', pages={pages_per_tag}")
            for page in range(1, pages_per_tag + 1):
                tag_url = f"https://bandcamp.com/tag/{tag}?page={page}"
                try:
                    driver.get(tag_url)
                    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
                except Exception as exc:
                    print(f"Bandcamp: error loading {tag_url}: {exc}")
                    continue
                candidates = _bandcamp_collect_from_tag_page(driver, tag_url)
                raw_candidates.extend(candidates)
                for link in candidates:
                    profile_url = _bandcamp_resolve_artist_profile_url(link)
                    if not profile_url:
                        continue
                    key = profile_url.rstrip('/').lower()
                    if key in seen_profiles:
                        continue
                    seen_profiles.add(key)
                    candidate_profiles.append((profile_url, tag))
                if len(candidate_profiles) >= max_artists:
                    break
                time.sleep(random.uniform(1.0, 2.0))
            if len(candidate_profiles) >= max_artists:
                break
        print(f"Bandcamp: total candidate links found {len(raw_candidates)}")
        print(f"Bandcamp: total artist profiles resolved {len(candidate_profiles)}")
        actionable_count = 0
        for profile_url, tag in candidate_profiles:
            artist_dict = _bandcamp_parse_artist_profile(driver, profile_url)
            if not artist_dict:
                continue
            artist_dict["source_tag"] = tag
            if BANDCAMP_MIN_CONTACT_REQUIREMENT and not _bandcamp_is_actionable(artist_dict):
                continue
            contact_links = []
            website = artist_dict.get("website")
            if website:
                contact_links.append(website)
            socials = artist_dict.get("socials", {})
            for social_link in socials.values():
                if social_link:
                    contact_links.append(social_link)
            email_address = artist_dict.get("email")
            if email_address:
                contact_links.append(f"mailto:{email_address}")
            contact_links = list(dict.fromkeys([link for link in contact_links if link]))
            if not contact_links:
                continue
            profile_key = (
                artist_dict.get("artist_name", "").strip().lower(),
                artist_dict.get("profile_url", "").rstrip("/").lower()
            )
            if profile_key in seen_artist_profiles:
                continue
            seen_artist_profiles.add(profile_key)
            bandcamp_rows.append((
                artist_dict.get("artist_name", ""),
                artist_dict.get("location", ""),
                artist_dict.get("latest_release_title", ""),
                "",
                contact_links,
                "",
                ""
            ))
            enriched_rows.append({
                "Artist Name": artist_dict.get("artist_name", ""),
                "Profile URL": artist_dict.get("profile_url", ""),
                "Website": artist_dict.get("website", ""),
                "Email": artist_dict.get("email", ""),
                "Instagram": socials.get("instagram", ""),
                "Twitter": socials.get("twitter", ""),
                "Facebook": socials.get("facebook", ""),
                "Linktree": socials.get("linktree", ""),
                "YouTube": socials.get("youtube", ""),
                "Location": artist_dict.get("location", ""),
                "Latest Release": artist_dict.get("latest_release_title", ""),
                "Latest Release Date": artist_dict.get("latest_release_date", ""),
                "Source Tag": artist_dict.get("source_tag", "")
            })
            actionable_count += 1
            if actionable_count >= max_artists:
                break
            time.sleep(random.uniform(1.0, 2.0))
        print(f"Bandcamp: total actionable artists written {actionable_count}")
    finally:
        driver.quit()
    if bandcamp_rows:
        save_to_csv(bandcamp_rows, existing_csv)
    if enriched_rows:
        _bandcamp_write_enriched_csv(enriched_rows, existing_csv)

def _bandcamp_collect_from_tag_page(driver, tag_url) -> list[str]:
    """Return a list of candidate Bandcamp links from a tag page (album/track/artist)."""
    candidates = []
    try:
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        for anchor in soup.find_all('a', href=True):
            href = anchor['href']
            absolute = urljoin(tag_url, href)
            if not absolute:
                continue
            lower = absolute.lower()
            if "bandcamp.com" not in lower:
                continue
            if any(segment in lower for segment in ["/album", "/track"]) or ".bandcamp.com" in urlparse(absolute).netloc:
                candidates.append(absolute)
    except Exception as exc:
        print(f"Bandcamp: failed to collect links from {tag_url}: {exc}")
    return candidates

def _bandcamp_resolve_artist_profile_url(candidate_url: str) -> str:
    """Normalize candidate links and resolve to canonical artist profile (https://artistname.bandcamp.com/)."""
    if not candidate_url:
        return ""
    url = candidate_url.strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if not url.startswith("http"):
        url = f"https://{url.lstrip('/')}"
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host.endswith("bandcamp.com"):
        return ""
    if host == "bandcamp.com":
        return ""
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}/"

def _bandcamp_parse_artist_profile(driver, profile_url) -> dict:
    """Visit artist profile; return dict with name, location, website, socials list, latest release title/date if visible."""
    artist = {
        "artist_name": "",
        "profile_url": profile_url,
        "location": "",
        "website": "",
        "email": "",
        "socials": {
            "instagram": "",
            "twitter": "",
            "facebook": "",
            "youtube": "",
            "linktree": "",
            "spotify": "",
            "bandsintown": "",
            "songkick": ""
        },
        "latest_release_title": "",
        "latest_release_date": "",
        "source_tag": ""
    }
    try:
        driver.get(profile_url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    except Exception as exc:
        print(f"Bandcamp: unable to load profile {profile_url}: {exc}")
        return {}
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    title_meta = soup.find('meta', attrs={'property': 'og:title'})
    if title_meta and title_meta.get('content'):
        artist["artist_name"] = title_meta['content'].split(' · ')[0].strip()
    if not artist["artist_name"]:
        name_el = soup.find(['h1', 'h2'], class_=re.compile('band-name|title', re.I))
        if name_el:
            artist["artist_name"] = name_el.get_text(strip=True)
    location_el = soup.find(class_=re.compile('location', re.I))
    if location_el:
        artist["location"] = location_el.get_text(" ", strip=True)
    if not artist["location"]:
        bio_el = soup.find('div', class_=re.compile('location', re.I))
        if bio_el:
            artist["location"] = bio_el.get_text(" ", strip=True)
    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].strip()
        if href.startswith("mailto:"):
            email_value = href.split("mailto:")[-1].split("?")[0]
            if email_value:
                artist["email"] = email_value
            continue
        normalized = href.split('#')[0]
        if normalized.startswith("//"):
            normalized = f"https:{normalized}"
        if normalized.startswith("/"):
            normalized = urljoin(profile_url, normalized)
        parsed = urlparse(normalized)
        if not parsed.scheme.startswith("http"):
            continue
        netloc = parsed.netloc.lower()
        if netloc.endswith("bandcamp.com"):
            continue
        if "instagram.com" in netloc:
            artist["socials"]["instagram"] = normalized
        elif "facebook.com" in netloc or "fb.me" in netloc:
            artist["socials"]["facebook"] = normalized
        elif "twitter.com" in netloc or "x.com" in netloc:
            artist["socials"]["twitter"] = normalized
        elif "youtube.com" in netloc or "youtu.be" in netloc:
            artist["socials"]["youtube"] = normalized
        elif any(domain in netloc for domain in ["linktr.ee", "linktree", "withkoji.com", "beacons.ai"]):
            artist["socials"]["linktree"] = normalized
        elif "spotify.com" in netloc:
            artist["socials"]["spotify"] = normalized
        elif "bandsintown.com" in netloc:
            artist["socials"]["bandsintown"] = normalized
        elif "songkick.com" in netloc:
            artist["socials"]["songkick"] = normalized
        else:
            if not artist["website"]:
                artist["website"] = normalized
    release_container = soup.find('li', class_=re.compile('music-grid-item', re.I))
    if release_container:
        title_el = release_container.find(class_=re.compile('title', re.I))
        if title_el:
            artist["latest_release_title"] = title_el.get_text(strip=True)
        date_el = release_container.find(class_=re.compile('release', re.I))
        if date_el:
            artist["latest_release_date"] = date_el.get_text(strip=True)
    if not artist["latest_release_title"]:
        track_title = soup.find(class_=re.compile('trackTitle', re.I))
        if track_title:
            artist["latest_release_title"] = track_title.get_text(strip=True)
    if not artist["latest_release_date"]:
        release_text = soup.find(class_=re.compile('release-date', re.I))
        if release_text:
            artist["latest_release_date"] = release_text.get_text(strip=True)
    if not artist["latest_release_date"]:
        credits = soup.find('div', class_=re.compile(r'tralbum-credits', re.I))
        if credits:
            credits_text = credits.get_text(" ", strip=True)
            match = re.search(r"released\s+(.+)", credits_text, re.I)
            if match:
                artist["latest_release_date"] = match.group(1).strip()
            else:
                artist["latest_release_date"] = credits_text.strip()
    if not artist["latest_release_date"]:
        artist["latest_release_date"] = "not present"
    return artist

def _bandcamp_is_actionable(artist_dict: dict) -> bool:
    """Return True if website or email or at least one social exists."""
    if not artist_dict:
        return False
    socials = artist_dict.get("socials", {})
    has_social = any(value for value in socials.values())
    return bool(artist_dict.get("website") or artist_dict.get("email") or has_social)

def _bandcamp_write_enriched_csv(rows, existing_csv):
    columns = [
        "Artist Name",
        "Profile URL",
        "Website",
        "Email",
        "Instagram",
        "Twitter",
        "Facebook",
        "Linktree",
        "YouTube",
        "Location",
        "Latest Release",
        "Latest Release Date",
        "Source Tag"
    ]
    base_dir = os.path.dirname(os.path.abspath(existing_csv))
    enriched_path = os.path.join(base_dir, "bandcamp_enriched.csv")
    existing_df = pd.DataFrame(columns=columns)
    if os.path.exists(enriched_path):
        try:
            existing_df = pd.read_csv(enriched_path)
        except Exception:
            existing_df = pd.DataFrame(columns=columns)
    for col in columns:
        if col not in existing_df.columns:
            existing_df[col] = ""
    if not existing_df.empty:
        existing_df = existing_df[columns]
    new_df = pd.DataFrame(rows)
    for col in columns:
        if col not in new_df.columns:
            new_df[col] = ""
    combined = pd.concat([existing_df, new_df], ignore_index=True)
    combined["__dedupe_key"] = (
        combined["Artist Name"].fillna("").str.strip().str.lower()
        + "||" +
        combined["Profile URL"].fillna("").str.rstrip("/").str.lower()
    )
    combined = combined.drop_duplicates(subset="__dedupe_key")
    combined = combined.drop(columns="__dedupe_key")
    combined = combined[columns]
    combined.to_csv(enriched_path, index=False)
# =============================================================================
# Facebook Scraping Functions (Page 2)
# =============================================================================
def extract_emails(text):
    email_pattern = r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
    return list(set(re.findall(email_pattern, text)))

def login_facebook(driver, fb_username, fb_password):
    driver.get('https://www.facebook.com/')
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, 'email')))
    driver.find_element(By.ID, 'email').send_keys(fb_username)
    driver.find_element(By.ID, 'pass').send_keys(fb_password)
    driver.find_element(By.NAME, 'login').click()
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))

def _extract_social_link_from_row(row):
    """Return the first usable social/Facebook URL from various column headers."""
    candidate_columns = [
        "Social Link",
        "social link",
        "SOCIAL LINK",
        "Facebook",
        "facebook",
        "FACEBOOK"
    ]
    for col in candidate_columns:
        if col in row and pd.notna(row[col]):
            value = str(row[col]).strip()
            if value:
                return value
    return ""

# =============================================================================
# UPDATED scrape_csv Function (Simpler Version with Wait Times of 0.5 sec and Session Refresh every 20 pages)
# =============================================================================
def scrape_csv(input_csv, output_csv, fb_username, fb_password, max_emails=None):
    existing_data = pd.DataFrame()
    if os.path.exists(output_csv):
        existing_data = pd.read_csv(output_csv)
    data = pd.read_csv(input_csv)
    # Normalize column names to remove extra whitespace.
    data.columns = [col.strip() for col in data.columns]
    results = []
    emails_found = 0
    processed_urls = set(existing_data['url'].tolist() if not existing_data.empty else [])
    exclude_urls = {"https://www.facebook.com/triplejunearthed/", "https://www.facebook.com/abc/"}
    facebook_rows = []
    for index, row in data.iterrows():
        url = _extract_social_link_from_row(row)
        if not url:
            continue
        if url in exclude_urls or url in processed_urls:
            continue
        if 'facebook.com' in url:
            facebook_rows.append(row)
    if not facebook_rows:
        print("No Facebook pages to process.")
        return
    driver = setup_facebook_driver()
    login_facebook(driver, fb_username, fb_password)
    session_counter = 0
    for row in facebook_rows:
        url = _extract_social_link_from_row(row)
        if not url:
            continue
        preexisting_emails = []
        if 'Email' in row and pd.notna(row['Email']):
            preexisting_emails = extract_emails(str(row['Email']))
        try:
            print(f"Scraping Facebook page: {url}")
            driver.get(url)
            session_counter += 1
            # Refresh the session every 20 pages.
            if session_counter % 20 == 0:
                print("Refreshing Facebook session...")
                driver.quit()
                driver = setup_facebook_driver()
                login_facebook(driver, fb_username, fb_password)
            try:
                # Wait up to 0.5 seconds for the About button.
                about_button = WebDriverWait(driver, 0.5).until(
                    EC.element_to_be_clickable((By.XPATH, '//a[@href="#about"]'))
                )
                print("About button found. Clicking...")
                about_button.click()
            except Exception as e:
                print(f"Error navigating to 'About' section on {url}: {e}")
            # Wait up to 0.5 seconds for the page body.
            WebDriverWait(driver, 0.5).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            emails = list(preexisting_emails)
            for span in soup.find_all('span', class_=re.compile('.*x193iq5w.*')):
                email = span.get_text(strip=True)
                if email:
                    emails.extend(extract_emails(email))
            unique_emails = sorted(set(email.strip() for email in emails if email))
            if unique_emails:
                # Format artist name: replace hyphens with spaces and capitalise each word.
                artist_name = row.get('Artist Name', '')
                artist_name = artist_name.replace('-', ' ').title()
                song_title = row.get('Song Title', '')
                if (not song_title) and ('Latest Release' in row):
                    song_title = row.get('Latest Release', '')
                results.append({
                    'artist': artist_name,
                    'location': row.get('Location', ''),
                    'song_title': song_title,
                    'sounds_like': row.get('Sounds Like', ''),
                    'url': url,
                    'emails': ', '.join(unique_emails),
                    'Played on triple J': row.get('Played on triple J', ''),
                    'Played on Unearthed': row.get('Played on Unearthed', ''),
                    'date_added': datetime.datetime.now().strftime("%Y-%m-%d")
                })
                emails_found += len(unique_emails)
                if max_emails is not None and emails_found >= max_emails:
                    break
        except Exception as e:
            print(f"Error scraping {url}: {e}")
        # Random sleep between 1 and 2 seconds.
        time.sleep(random.uniform(1, 2))
    driver.quit()
    results_df = pd.DataFrame(results)
    combined_data = pd.concat([existing_data, results_df]).drop_duplicates(subset=['url', 'emails'])
    combined_data.to_csv(output_csv, index=False)
    print(f"Scraping completed. Results saved to {output_csv}")

# =============================================================================
# PyQt5 GUI Code
# =============================================================================
class ArtistScraperThread(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal()
    def __init__(self, website_url, max_artists, output_csv, source="Unearthed",
                 pages_per_tag=BANDCAMP_PAGES_PER_TAG, seed_tags=None, parent=None):
        super().__init__(parent)
        self.website_url = website_url
        self.max_artists = max_artists
        self.output_csv = output_csv
        self.source = source
        self.pages_per_tag = pages_per_tag
        self.seed_tags = list(seed_tags) if seed_tags else list(BANDCAMP_SEED_TAGS)
    def run(self):
        self.log_signal.emit("Starting artist scraping...")
        try:
            if self.source.lower() == "bandcamp":
                scrape_bandcamp(
                    self.seed_tags,
                    pages_per_tag=self.pages_per_tag,
                    existing_csv=self.output_csv,
                    max_artists=self.max_artists
                )
                self.log_signal.emit("Bandcamp scraping completed.")
            else:
                scrape_website(self.website_url, existing_csv=self.output_csv, max_artists=self.max_artists)
                self.log_signal.emit("Artist scraping completed.")
        except Exception as e:
            self.log_signal.emit(f"Error in artist scraping: {e}")
        self.finished_signal.emit()

class FacebookScraperThread(QtCore.QThread):
    log_signal = QtCore.pyqtSignal(str)
    finished_signal = QtCore.pyqtSignal()
    def __init__(self, input_csv, output_csv, fb_username, fb_password, max_emails, parent=None):
        super().__init__(parent)
        self.input_csv = input_csv
        self.output_csv = output_csv
        self.fb_username = fb_username
        self.fb_password = fb_password
        self.max_emails = max_emails
    def run(self):
        self.log_signal.emit("Starting Facebook scraping...")
        try:
            scrape_csv(self.input_csv, self.output_csv, self.fb_username, self.fb_password, self.max_emails)
            self.log_signal.emit("Facebook scraping completed.")
        except Exception as e:
            self.log_signal.emit(f"Error in Facebook scraping: {e}")
        self.finished_signal.emit()

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Artist & Facebook Scraper")
        self.setMinimumSize(800, 600)
        self.create_menu()
        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)
        self.artist_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.artist_tab, "Artist Scraping")
        self.create_artist_tab()
        self.facebook_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.facebook_tab, "Facebook Scraping")
        self.create_facebook_tab()
    def create_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        save_as_action = QtWidgets.QAction("Save As (Facebook Output)...", self)
        save_as_action.triggered.connect(self.save_as_facebook_csv)
        file_menu.addAction(save_as_action)
    def save_as_facebook_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Facebook Output CSV As", "", "CSV Files (*.csv)")
        if file_path:
            self.output_csv_edit.setText(file_path)
    def browse_artist_output_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Select Artist Output CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.artist_output_csv_edit.setText(file_path)
    def browse_input_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select Input CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.input_csv_edit.setText(file_path)
    def browse_output_csv(self):
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Select Facebook Output CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.output_csv_edit.setText(file_path)
    def create_artist_tab(self):
        layout = QtWidgets.QVBoxLayout()
        source_layout = QtWidgets.QHBoxLayout()
        source_label = QtWidgets.QLabel("Source:")
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.addItems(["Unearthed", "Bandcamp"])
        self.source_combo.currentTextChanged.connect(self.on_source_changed)
        source_layout.addWidget(source_label)
        source_layout.addWidget(self.source_combo)
        layout.addLayout(source_layout)
        url_layout = QtWidgets.QHBoxLayout()
        url_label = QtWidgets.QLabel("Website URL:")
        self.url_edit = QtWidgets.QLineEdit(UNEARTHED_DEFAULT_URL)
        self.url_edit.setPlaceholderText(UNEARTHED_DEFAULT_URL)
        url_layout.addWidget(url_label)
        url_layout.addWidget(self.url_edit)
        layout.addLayout(url_layout)
        pages_layout = QtWidgets.QHBoxLayout()
        pages_label = QtWidgets.QLabel("Pages per Tag:")
        self.pages_per_tag_edit = QtWidgets.QLineEdit(str(BANDCAMP_PAGES_PER_TAG))
        self.pages_per_tag_edit.setEnabled(False)
        pages_layout.addWidget(pages_label)
        pages_layout.addWidget(self.pages_per_tag_edit)
        layout.addLayout(pages_layout)
        max_artists_layout = QtWidgets.QHBoxLayout()
        max_artists_label = QtWidgets.QLabel("Max Artists:")
        self.max_artists_edit = QtWidgets.QLineEdit("200")
        max_artists_layout.addWidget(max_artists_label)
        max_artists_layout.addWidget(self.max_artists_edit)
        layout.addLayout(max_artists_layout)
        artist_output_layout = QtWidgets.QHBoxLayout()
        artist_output_label = QtWidgets.QLabel("Output CSV:")
        self.artist_output_csv_edit = QtWidgets.QLineEdit("artist_social_links.csv")
        artist_output_browse = QtWidgets.QPushButton("Browse")
        artist_output_browse.clicked.connect(self.browse_artist_output_csv)
        artist_output_layout.addWidget(artist_output_label)
        artist_output_layout.addWidget(self.artist_output_csv_edit)
        artist_output_layout.addWidget(artist_output_browse)
        layout.addLayout(artist_output_layout)
        self.artist_start_button = QtWidgets.QPushButton("Start Artist Scraping")
        self.artist_start_button.clicked.connect(self.start_artist_scraping)
        layout.addWidget(self.artist_start_button)
        self.artist_progress_bar = QtWidgets.QProgressBar()
        self.artist_progress_bar.setRange(0, 0)
        self.artist_progress_bar.setVisible(False)
        layout.addWidget(self.artist_progress_bar)
        self.artist_log = QtWidgets.QTextEdit()
        self.artist_log.setReadOnly(True)
        layout.addWidget(self.artist_log)
        self.artist_tab.setLayout(layout)
    def on_source_changed(self, source_text):
        if source_text == "Bandcamp":
            self.url_edit.setPlaceholderText(BANDCAMP_DEFAULT_TAG_URL)
            current = self.url_edit.text().strip()
            if not current or current == UNEARTHED_DEFAULT_URL:
                self.url_edit.setText(BANDCAMP_DEFAULT_TAG_URL)
            self.pages_per_tag_edit.setEnabled(True)
        else:
            self.url_edit.setPlaceholderText(UNEARTHED_DEFAULT_URL)
            current = self.url_edit.text().strip()
            if not current or current == BANDCAMP_DEFAULT_TAG_URL:
                self.url_edit.setText(UNEARTHED_DEFAULT_URL)
            self.pages_per_tag_edit.setEnabled(False)
    def create_facebook_tab(self):
        layout = QtWidgets.QVBoxLayout()
        input_layout = QtWidgets.QHBoxLayout()
        input_label = QtWidgets.QLabel("Input CSV:")
        self.input_csv_edit = QtWidgets.QLineEdit("test_artist_social_links.csv")
        input_browse = QtWidgets.QPushButton("Browse")
        input_browse.clicked.connect(self.browse_input_csv)
        input_layout.addWidget(input_label)
        input_layout.addWidget(self.input_csv_edit)
        input_layout.addWidget(input_browse)
        layout.addLayout(input_layout)
        output_layout = QtWidgets.QHBoxLayout()
        output_label = QtWidgets.QLabel("Output CSV:")
        self.output_csv_edit = QtWidgets.QLineEdit("test_combined_artist_data.csv")
        output_browse = QtWidgets.QPushButton("Browse")
        output_browse.clicked.connect(self.browse_output_csv)
        output_layout.addWidget(output_label)
        output_layout.addWidget(self.output_csv_edit)
        output_layout.addWidget(output_browse)
        layout.addLayout(output_layout)
        fb_layout = QtWidgets.QHBoxLayout()
        fb_user_label = QtWidgets.QLabel("Facebook Username:")
        self.fb_username_edit = QtWidgets.QLineEdit()
        fb_pass_label = QtWidgets.QLabel("Facebook Password:")
        self.fb_password_edit = QtWidgets.QLineEdit()
        self.fb_password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        fb_layout.addWidget(fb_user_label)
        fb_layout.addWidget(self.fb_username_edit)
        fb_layout.addWidget(fb_pass_label)
        fb_layout.addWidget(self.fb_password_edit)
        layout.addLayout(fb_layout)
        max_emails_layout = QtWidgets.QHBoxLayout()
        max_emails_label = QtWidgets.QLabel("Max Emails (optional):")
        self.max_emails_edit = QtWidgets.QLineEdit()
        max_emails_layout.addWidget(max_emails_label)
        max_emails_layout.addWidget(self.max_emails_edit)
        layout.addLayout(max_emails_layout)
        self.fb_start_button = QtWidgets.QPushButton("Start Facebook Scraping")
        self.fb_start_button.clicked.connect(self.start_facebook_scraping)
        layout.addWidget(self.fb_start_button)
        self.fb_progress_bar = QtWidgets.QProgressBar()
        self.fb_progress_bar.setRange(0, 0)
        self.fb_progress_bar.setVisible(False)
        layout.addWidget(self.fb_progress_bar)
        self.fb_log = QtWidgets.QTextEdit()
        self.fb_log.setReadOnly(True)
        layout.addWidget(self.fb_log)
        self.facebook_tab.setLayout(layout)
    def start_artist_scraping(self):
        source = self.source_combo.currentText()
        url = self.url_edit.text().strip()
        if source == "Bandcamp" and not url:
            url = BANDCAMP_DEFAULT_TAG_URL
            self.url_edit.setText(url)
        if source != "Bandcamp" and not url:
            self.artist_log.append("Please enter a valid website URL.")
            return
        try:
            max_artists = int(self.max_artists_edit.text().strip())
        except ValueError:
            max_artists = 200
        try:
            pages_per_tag = int(self.pages_per_tag_edit.text().strip())
        except ValueError:
            pages_per_tag = BANDCAMP_PAGES_PER_TAG
        if max_artists <= 0:
            max_artists = 200
        if pages_per_tag <= 0:
            pages_per_tag = BANDCAMP_PAGES_PER_TAG
        seed_tags = list(BANDCAMP_SEED_TAGS)
        if source == "Bandcamp":
            extracted_tag = _bandcamp_extract_tag_from_url(url)
            if extracted_tag:
                seed_tags = [extracted_tag]
        output_csv = self.artist_output_csv_edit.text().strip()
        self.artist_start_button.setEnabled(False)
        self.artist_progress_bar.setVisible(True)
        self.artist_log.append("Initiating artist scraping...")
        self.artist_thread = ArtistScraperThread(
            url,
            max_artists,
            output_csv,
            source=source,
            pages_per_tag=pages_per_tag,
            seed_tags=seed_tags
        )
        self.artist_thread.log_signal.connect(self.update_artist_log)
        self.artist_thread.finished_signal.connect(self.artist_scraping_finished)
        self.artist_thread.start()
    def update_artist_log(self, message):
        self.artist_log.append(message)
    def artist_scraping_finished(self):
        self.artist_log.append("Artist scraping thread finished.")
        self.artist_progress_bar.setVisible(False)
        self.artist_start_button.setEnabled(True)
    def start_facebook_scraping(self):
        input_csv = self.input_csv_edit.text().strip()
        output_csv = self.output_csv_edit.text().strip()
        fb_username = self.fb_username_edit.text().strip()
        fb_password = self.fb_password_edit.text().strip()
        max_emails_text = self.max_emails_edit.text().strip()
        max_emails = int(max_emails_text) if max_emails_text.isdigit() else None
        if not input_csv or not output_csv or not fb_username or not fb_password:
            self.fb_log.append("Please fill in all required fields for Facebook scraping.")
            return
        self.fb_start_button.setEnabled(False)
        self.fb_progress_bar.setVisible(True)
        self.fb_log.append("Initiating Facebook scraping...")
        self.fb_thread = FacebookScraperThread(input_csv, output_csv, fb_username, fb_password, max_emails)
        self.fb_thread.log_signal.connect(self.update_fb_log)
        self.fb_thread.finished_signal.connect(self.facebook_scraping_finished)
        self.fb_thread.start()
    def update_fb_log(self, message):
        self.fb_log.append(message)
    def facebook_scraping_finished(self):
        self.fb_log.append("Facebook scraping thread finished.")
        self.fb_progress_bar.setVisible(False)
        self.fb_start_button.setEnabled(True)

if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

# ---------------------------
# Stop caffeinate if it was started (macOS)
# ---------------------------
if caffeinate_proc:
    print("Stopping caffeinate. You may now allow sleep.")
    caffeinate_proc.kill()
