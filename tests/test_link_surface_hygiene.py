from types import SimpleNamespace

import pandas as pd
import pytest

from link_surface_hygiene import (
    is_artist_link_hub_profile,
    is_artist_platform_profile,
    is_useful_artist_link,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://linktr.ee/luckyiris",
        "https://instagram.com/luckyiris",
        "https://soundcloud.com/luckyiris",
        "https://luckyiris.example/",
        "https://threads.net/@luckyiris",
        "https://open.spotify.com/artist/lucky-iris-id",
        "https://youtube.com/@luckyiris",
        "https://discord.gg/luckyiris",
    ],
)
def test_artist_operational_destinations_are_retained(url):
    if url == "https://linktr.ee/luckyiris":
        assert is_useful_artist_link(url)
    else:
        assert is_useful_artist_link(url, from_link_hub=True, source_hub_url="https://linktr.ee/luckyiris")


@pytest.mark.parametrize(
    "url",
    [
        "https://apps.apple.com/app/linktree-link-in-bio-creator/id1454431657",
        "https://play.google.com/store/apps/details?id=ee.linktr.admin",
        "https://instagram.com/linktree",
        "https://threads.net/@linktree",
        "https://discord.gg/linktree",
        "https://linktr.ee/privacy",
        "https://linktr.ee/terms",
        "https://linktr.ee/help",
        "https://linktr.ee/login",
        "https://linktr.ee/signup",
        "https://click.linktr.ee/track/campaign",
        "https://linktr.ee/discover/music",
        "https://linktr.ee/unrelated-public-profile",
        "https://instagram.com/",
        "https://youtube.com/watch?v=not-an-artist-profile",
    ],
)
def test_linktree_shell_and_generic_platform_destinations_are_rejected(url):
    assert not is_useful_artist_link(url, from_link_hub=True, source_hub_url="https://linktr.ee/luckyiris")


def test_linktree_profile_and_artist_platform_classification_are_specific():
    assert is_artist_link_hub_profile("https://linktr.ee/luckyiris")
    assert not is_artist_link_hub_profile("https://linktr.ee/discover")
    assert is_artist_platform_profile("https://soundcloud.com/luckyiris")
    assert is_artist_platform_profile("https://threads.net/@luckyiris")
    assert is_artist_platform_profile("https://threads.net/@linktree")
    assert not is_useful_artist_link(
        "https://threads.net/@linktree",
        from_link_hub=True,
        source_hub_url="https://linktr.ee/luckyiris",
    )
    assert not is_useful_artist_link(
        "https://discord.gg/random-company-invite",
        from_link_hub=True,
        source_hub_url="https://linktr.ee/luckyiris",
        anchor_context="Join the Linktree Discord community",
    )


def test_lucky_iris_linktree_one_hop_payload_stays_clean_and_bounded(monkeypatch):
    pytest.importorskip("PyQt5")
    import cross_directory_enricher as cde

    hub_url = "https://linktr.ee/luckyiris"
    official_url = "https://luckyiris.example/"
    hub_html = """
      <a href="https://instagram.com/luckyiris">Instagram</a>
      <a href="https://soundcloud.com/luckyiris">SoundCloud</a>
      <a href="https://threads.net/@luckyiris">Threads</a>
      <a href="https://discord.gg/luckyiris">Discord</a>
      <a href="https://luckyiris.example/">Official website</a>
      <a href="https://apps.apple.com/app/linktree-link-in-bio-creator/id1454431657">App Store</a>
      <a href="https://play.google.com/store/apps/details?id=ee.linktr.admin">Google Play</a>
      <a href="https://instagram.com/linktree">Linktree Instagram</a>
      <a href="https://threads.net/@linktree">Linktree Threads</a>
      <a href="https://discord.gg/linktree">Linktree Discord</a>
      <a href="https://discord.gg/company-community" aria-label="Join the Linktree Discord">Community</a>
      <a href="https://linktr.ee/privacy">Privacy</a>
      <a href="https://linktr.ee/discover/music">Discover</a>
      <a href="https://click.linktr.ee/track/campaign">Tracking</a>
    """

    class Response:
        text = hub_html

        def raise_for_status(self):
            return None

    worker = cde.CrossDirectoryEnricherWorker("seed.csv", "output.csv", enable_live_search=False)
    fetches = []

    def fetch(url, **_kwargs):
        fetches.append(url)
        return Response()

    worker.session = SimpleNamespace(get=fetch)
    worker.log_message = SimpleNamespace(emit=lambda _message: None)
    frame = pd.DataFrame(
        [{
            "Artist Name": "Lucky Iris",
            "Social Link": "",
            "External Links": hub_url,
            "Email": "",
            "Website": "",
        }],
        dtype=str,
    )

    changed = worker._expand_spotify_link_hubs(frame, 0, {"artist": "Lucky Iris"})

    social_links = set(cde._split_multi_value(frame.at[0, "Social Link"]))
    external_links = set(cde._split_multi_value(frame.at[0, "External Links"]))
    assert changed is True
    assert fetches == [hub_url]
    assert {
        "https://instagram.com/luckyiris",
        "https://soundcloud.com/luckyiris",
        "https://threads.net/@luckyiris",
        "https://discord.gg/luckyiris",
    }.issubset(social_links)
    assert external_links == {hub_url, official_url}
    assert len(social_links) <= cde.MAX_LINK_HUB_SOCIALS_PER_ROW
    assert len(external_links) <= cde.MAX_WEBSITES
    assert frame.at[0, "Website"] == ""
    assert frame.at[0, "Email"] == ""
    combined = " ".join(social_links | external_links).lower()
    assert "apps.apple.com" not in combined
    assert "play.google.com" not in combined
    assert "linktree" not in combined.replace(hub_url, "")
    assert "privacy" not in combined
    assert "discover" not in combined
    assert "click.linktr.ee" not in combined


def test_lead_vault_consolidation_does_not_reintroduce_linktree_shell():
    from lead_vault.merge import _coalesce_consolidation_enrichment
    from lead_vault.schema import get_canonical_master_schema

    winner = {field: "" for field in get_canonical_master_schema()}
    loser = dict(winner)
    winner.update(
        {
            "Artist": "Lucky Iris",
            "External_Links": "https://linktr.ee/luckyiris;https://luckyiris.example/",
            "Social Link": "https://instagram.com/luckyiris, https://linktr.ee/luckyiris",
        }
    )
    loser.update(
        {
            "Artist": "Lucky Iris",
            "External_Links": ";".join(
                [
                    "https://apps.apple.com/app/linktree/id1454431657",
                    "https://play.google.com/store/apps/details?id=ee.linktr.admin",
                    "https://linktr.ee/privacy",
                    "https://click.linktr.ee/track/campaign",
                    "https://linktr.ee/unrelated-public-profile",
                ]
            ),
            "Social Link": ";".join(
                [
                    "https://soundcloud.com/luckyiris",
                    "https://instagram.com/linktree",
                    "https://threads.net/@linktree",
                ]
            ),
        }
    )

    merged = _coalesce_consolidation_enrichment(winner, loser)

    assert set(merged["External_Links"].split(";")) == {
        "https://linktr.ee/luckyiris",
        "https://luckyiris.example/",
    }
    assert set(merged["Social Link"].split(";")) == {
        "https://instagram.com/luckyiris",
        "https://linktr.ee/luckyiris",
        "https://soundcloud.com/luckyiris",
    }
