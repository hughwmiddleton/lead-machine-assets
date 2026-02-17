from night_mode_v2.identity_resolver import resolve_identity_keys


def test_spotify_identity_priority():
    row = {"Spotify_Artist_ID": "12345"}
    keys = resolve_identity_keys(row)
    assert keys[0] == "spotify:12345"


def test_bandcamp_subdomain_extracted():
    row = {"Source URL": "https://coolband.bandcamp.com/album/good-times"}
    keys = resolve_identity_keys(row)
    assert "bandcamp:coolband" in keys


def test_soundcloud_handle_extracted():
    row = {"SoundCloud Link": "https://soundcloud.com/greatartist/track"}
    keys = resolve_identity_keys(row)
    assert "soundcloud:greatartist" in keys


def test_nameurl_fallback_uses_external_link():
    row = {
        "Artist Name": "My Band!",
        "External Links": "http://example.com;http://second.com",
    }
    keys = resolve_identity_keys(row)
    assert "nameurl:my band|http://example.com" in keys
