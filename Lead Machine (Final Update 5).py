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
from PyQt5 import QtWidgets, QtCore

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
        url = row['Social Link']
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
        url = row['Social Link']
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
            emails = []
            for span in soup.find_all('span', class_=re.compile('.*x193iq5w.*')):
                email = span.get_text(strip=True)
                if email:
                    emails.extend(extract_emails(email))
            if emails:
                # Format artist name: replace hyphens with spaces and capitalise each word.
                artist_name = row.get('Artist Name', '')
                artist_name = artist_name.replace('-', ' ').title()
                results.append({
                    'artist': artist_name,
                    'location': row.get('Location', ''),
                    'song_title': row.get('Song Title', ''),
                    'sounds_like': row.get('Sounds Like', ''),
                    'url': url,
                    'emails': ', '.join(emails),
                    'Played on triple J': row.get('Played on triple J', ''),
                    'Played on Unearthed': row.get('Played on Unearthed', ''),
                    'date_added': datetime.datetime.now().strftime("%Y-%m-%d")
                })
                emails_found += len(emails)
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
    def __init__(self, website_url, max_artists, output_csv, parent=None):
        super().__init__(parent)
        self.website_url = website_url
        self.max_artists = max_artists
        self.output_csv = output_csv
    def run(self):
        self.log_signal.emit("Starting artist scraping...")
        try:
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
        url_layout = QtWidgets.QHBoxLayout()
        url_label = QtWidgets.QLabel("Website URL:")
        self.url_edit = QtWidgets.QLineEdit("https://www.abc.net.au/triplejunearthed/music/")
        url_layout.addWidget(url_label)
        url_layout.addWidget(self.url_edit)
        layout.addLayout(url_layout)
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
        url = self.url_edit.text().strip()
        if not url:
            self.artist_log.append("Please enter a valid website URL.")
            return
        try:
            max_artists = int(self.max_artists_edit.text().strip())
        except ValueError:
            max_artists = 200
        output_csv = self.artist_output_csv_edit.text().strip()
        self.artist_start_button.setEnabled(False)
        self.artist_progress_bar.setVisible(True)
        self.artist_log.append("Initiating artist scraping...")
        self.artist_thread = ArtistScraperThread(url, max_artists, output_csv)
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
