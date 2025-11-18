"""One-time Spotify OAuth helper to capture a refresh token for Lead Machine."""
from __future__ import annotations

import os
import sys
import urllib.parse
import webbrowser

import requests


AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
]

DEFAULT_CLIENT_ID = "d32944f1a2414cd7a1681b4759f6a402"
DEFAULT_CLIENT_SECRET = "27188b55b8d94604a9a2172092e19416"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8080/callback"


def prompt_for_redirect_url() -> str:
    print("\nWhen Spotify redirects you, copy the full URL from your browser's address bar")
    return input("and paste it here: ").strip()


def main() -> None:
    client_id = (os.getenv("SPOTIFY_CLIENT_ID") or DEFAULT_CLIENT_ID).strip()
    client_secret = (os.getenv("SPOTIFY_CLIENT_SECRET") or DEFAULT_CLIENT_SECRET).strip()
    redirect_uri = (os.getenv("SPOTIFY_REDIRECT_URI") or DEFAULT_REDIRECT_URI).strip()

    if not client_id or not client_secret or not redirect_uri:
        print("Missing Spotify OAuth configuration. Please set:")
        print("  SPOTIFY_CLIENT_ID")
        print("  SPOTIFY_CLIENT_SECRET")
        print("  SPOTIFY_REDIRECT_URI (e.g. http://localhost:8080/callback)")
        sys.exit(1)

    scope_str = " ".join(SCOPES)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope_str,
        "show_dialog": "true",
    }
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    auth_url = f"{AUTHORIZE_URL}?{query}"

    print("\nOpen the following URL in your browser to authorize Lead Machine:")
    print(auth_url)
    print()
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    redirect_response = prompt_for_redirect_url()
    if not redirect_response:
        print("No redirect URL provided; cannot continue.")
        sys.exit(1)

    parsed = urllib.parse.urlparse(redirect_response)
    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        print("The provided URL did not contain a 'code' parameter. Please try again.")
        sys.exit(1)

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    response = requests.post(TOKEN_URL, data=data, timeout=15)
    if response.status_code != 200:
        print("Token exchange failed:")
        print(response.status_code, response.text)
        sys.exit(1)

    payload = response.json()
    refresh_token = payload.get("refresh_token")
    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")

    if not refresh_token:
        print("Response did not include a refresh_token. Ensure the app allows it and try again.")
        sys.exit(1)

    print("\nSUCCESS! Keep this refresh token secret.")
    print("Add the following line to your environment (e.g. .env or shell profile):\n")
    print(f"SPOTIFY_REFRESH_TOKEN={refresh_token}")
    print("\nAccess token (temporary):", access_token)
    print("Expires in:", expires_in, "seconds")


if __name__ == "__main__":
    main()
DEFAULT_CLIENT_ID = "d32944f1a2414cd7a1681b4759f6a402"
DEFAULT_CLIENT_SECRET = "27188b55b8d94604a9a2172092e19416"
DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"
