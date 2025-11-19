"""
Lightweight Spotify Web API client for Lead Machine.

Uses the client credentials flow for read-only endpoints such as playlist
tracks and artist metadata.
"""
import os
import time
from typing import Dict, List, Optional

import requests


def _clean_playlist_id(raw: Optional[str]) -> str:
    """Normalize playlist input to a bare Spotify playlist ID."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "spotify.com" in value:
        value = value.split("?", 1)[0]
        value = value.rstrip("/").split("/")[-1]
    return value


class SpotifyClient:
    API_BASE = "https://api.spotify.com/v1"
    TOKEN_URL = "https://accounts.spotify.com/api/token"

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        logger=None,
    ) -> None:
        self.client_id = (client_id or os.getenv("SPOTIFY_CLIENT_ID") or "").strip()
        self.client_secret = (client_secret or os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip()
        self.logger = logger
        self.session = requests.Session()
        self.refresh_token = (os.getenv("SPOTIFY_REFRESH_TOKEN") or "").strip()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._user_access_token: Optional[str] = None
        self._user_access_token_expires_at: float = 0.0

        if not self.client_id or not self.client_secret:
            raise ValueError("Spotify credentials not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")

    def _log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:
                pass

    def get_access_token(self) -> str:
        """
        Obtain (and cache) an application access token via client credentials.
        """
        now = time.time()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        response = self.session.post(
            self.TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
            timeout=10,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Spotify token request failed: {response.status_code} {response.text}")

        payload = response.json()
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in", 3600)
        if not access_token:
            raise RuntimeError("Spotify token response missing access_token")

        # Refresh slightly before actual expiry.
        self._access_token = access_token
        self._token_expires_at = now + max(int(expires_in) - 30, 60)
        return self._access_token

    def _get_user_access_token_from_refresh(self) -> str:
        """Use the stored refresh token to obtain a short-lived user access token."""
        if not self.refresh_token:
            raise RuntimeError("Spotify refresh token not configured.")

        now = time.time()
        if self._user_access_token and now < self._user_access_token_expires_at:
            return self._user_access_token

        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        response = self.session.post(self.TOKEN_URL, data=data, timeout=10)
        if response.status_code != 200:
            raise RuntimeError(f"Spotify refresh-token request failed: {response.status_code} {response.text}")

        payload = response.json()
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in", 3600)
        if not access_token:
            raise RuntimeError("Spotify refresh-token response missing access_token")

        self._user_access_token = access_token
        self._user_access_token_expires_at = now + max(int(expires_in) - 30, 60)
        return self._user_access_token

    def _auth_headers(self) -> Dict[str, str]:
        if self.refresh_token:
            token = self._get_user_access_token_from_refresh()
        else:
            token = self.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _authorized_request(self, method: str, url: str, **kwargs) -> Dict:
        """
        Perform an authorized HTTP request to the Spotify API.
        """
        headers = self._auth_headers()
        extra_headers = kwargs.pop("headers", {}) or {}
        headers.update(extra_headers)
        attempt = 0
        while True:
            attempt += 1
            response = self.session.request(method, url, headers=headers, timeout=15, **kwargs)
            if response.status_code == 429 and attempt < 3:
                retry_after = int(response.headers.get("Retry-After", "1"))
                time.sleep(max(retry_after, 1))
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"Spotify API error {response.status_code}: {response.text}")
            return response.json()

    def get_playlist_tracks(self, playlist_id: str, limit: int = 100, max_items: int = 500) -> List[Dict]:
        """
        Fetch raw playlist track entries up to max_items.
        """
        playlist_id = _clean_playlist_id(playlist_id)
        if not playlist_id:
            return []
        items: List[Dict] = []
        offset = 0
        limit = max(1, min(limit, 100))
        max_items = max_items if max_items and max_items > 0 else 500

        while offset < max_items:
            page_limit = min(limit, max_items - offset)
            url = f"{self.API_BASE}/playlists/{playlist_id}/tracks"
            params = {"offset": offset, "limit": page_limit}
            data = self._authorized_request("GET", url, params=params)
            page_items = data.get("items", []) or []
            if not page_items:
                break
            items.extend(page_items)
            offset += len(page_items)
            if not data.get("next"):
                break

        return items[:max_items]

    def get_artists_details(self, artist_ids: List[str]) -> Dict[str, Dict]:
        """
        Fetch artist objects in batches of 50 using /v1/artists.
        """
        if not artist_ids:
            return {}
        details: Dict[str, Dict] = {}
        unique_ids = []
        seen = set()
        for artist_id in artist_ids:
            if not artist_id or artist_id in seen:
                continue
            seen.add(artist_id)
            unique_ids.append(artist_id)

        for start in range(0, len(unique_ids), 50):
            chunk = unique_ids[start : start + 50]
            url = f"{self.API_BASE}/artists"
            params = {"ids": ",".join(chunk)}
            data = self._authorized_request("GET", url, params=params)
            for artist in data.get("artists", []) or []:
                artist_id = artist.get("id")
                if artist_id:
                    details[artist_id] = artist

        return details

    def get_playlist_metadata(self, playlist_id: str) -> Dict:
        """
        Fetch playlist metadata (e.g., name) for labeling rows.
        """
        playlist_id = _clean_playlist_id(playlist_id)
        if not playlist_id:
            return {}
        url = f"{self.API_BASE}/playlists/{playlist_id}"
        params = {"fields": "id,name"}
        return self._authorized_request("GET", url, params=params)
