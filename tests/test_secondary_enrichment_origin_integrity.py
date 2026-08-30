import pandas as pd
import pytest

from cross_directory_enricher import CrossDirectoryEnricherWorker, EnrichmentPayload
from source_scheduler import is_spotify_origin_row


ORIGIN_FIELDS = ("Lead_Source", "Source_Directory", "Source Directory", "Source URL", "Source_URL")


def _row(**overrides):
    row = {
        "Artist Name": "Origin Act",
        "Email": "",
        "Email_All": "",
        "Email_Source_URL": "",
        "Email_Source_Type": "",
        "Email_Extract_Method": "",
        "Email_Provenance_JSON": "",
        "Social Link": "",
        "External Links": "",
        "SoundCloud Link": "",
        "Bandcamp_URL": "",
        "Spotify_URL": "https://open.spotify.com/artist/spotify-origin-act",
        "Lead_Source": "Spotify",
        "Source_Directory": "Spotify",
        "Source Directory": "Spotify",
        "Source URL": "https://open.spotify.com/artist/spotify-origin-act",
        "Source_URL": "https://open.spotify.com/artist/spotify-origin-act",
        "final_status": "valid",
        "contact_safety": "safe",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("source_dir", "source_url", "socials", "websites", "enriched_field", "enriched_value"),
    [
        (
            "bandcamp",
            "https://origin-act.bandcamp.com/",
            set(),
            set(),
            "Bandcamp_URL",
            "https://origin-act.bandcamp.com",
        ),
        (
            "soundcloud",
            "https://soundcloud.com/origin-act",
            set(),
            set(),
            "SoundCloud Link",
            "https://soundcloud.com/origin-act",
        ),
        (
            "instagram_enrich",
            "https://www.instagram.com/origin_act/",
            {"https://www.instagram.com/origin_act/"},
            set(),
            "Social Link",
            "https://www.instagram.com/origin_act/",
        ),
        (
            "website_enrich",
            "https://origin-act.example/contact",
            set(),
            {"https://origin-act.example/"},
            "External Links",
            "https://origin-act.example/",
        ),
        (
            "facebook_enrich",
            "https://www.facebook.com/origin.act/about",
            {"https://www.facebook.com/origin.act"},
            set(),
            "Social Link",
            "https://www.facebook.com/origin.act",
        ),
    ],
)
def test_spotify_origin_survives_each_secondary_payload(
    source_dir,
    source_url,
    socials,
    websites,
    enriched_field,
    enriched_value,
):
    dataframe = pd.DataFrame([_row()])
    before = dataframe.loc[0, [*ORIGIN_FIELDS, "final_status", "contact_safety"]].to_dict()
    assert is_spotify_origin_row(dataframe.loc[0])
    payload = EnrichmentPayload(
        socials=socials,
        websites=websites,
        emails={"hello@origin-act.example"},
        source_dir=source_dir,
        source_url=source_url,
        match_score=1.0,
        candidate_name="Origin Act",
    )

    CrossDirectoryEnricherWorker._apply_payload(None, dataframe, 0, payload)

    assert dataframe.loc[0, [*ORIGIN_FIELDS, "final_status", "contact_safety"]].to_dict() == before
    assert dataframe.at[0, enriched_field] == enriched_value
    assert dataframe.at[0, "Email_Source_Type"] == source_dir
    assert dataframe.at[0, "Email_Source_URL"] == source_url


@pytest.mark.parametrize(
    ("lead_source", "source_directory", "source_url"),
    [
        ("Triple J Unearthed", "unearthed", "https://www.abc.net.au/triplejunearthed/artist/origin-act"),
        ("Bandcamp", "bandcamp", "https://origin-act.bandcamp.com/"),
        ("SoundCloud", "soundcloud", "https://soundcloud.com/origin-act"),
    ],
)
def test_non_spotify_discovery_origin_survives_secondary_payload(lead_source, source_directory, source_url):
    dataframe = pd.DataFrame(
        [
            _row(
                Spotify_URL="",
                Lead_Source=lead_source,
                Source_Directory=source_directory,
                **{"Source Directory": lead_source, "Source URL": source_url, "Source_URL": source_url},
            )
        ]
    )
    before = dataframe.loc[0, list(ORIGIN_FIELDS)].to_dict()

    CrossDirectoryEnricherWorker._apply_payload(
        None,
        dataframe,
        0,
        EnrichmentPayload(
            emails={"hello@origin-act.example"},
            source_dir="website_enrich",
            source_url="https://origin-act.example/contact",
        ),
    )

    assert dataframe.loc[0, list(ORIGIN_FIELDS)].to_dict() == before
    assert dataframe.at[0, "Email_Source_Type"] == "website_enrich"
    assert dataframe.at[0, "Email_Source_URL"] == "https://origin-act.example/contact"


def test_payload_boundary_restores_origin_even_when_application_raises(monkeypatch):
    dataframe = pd.DataFrame([_row()])
    before = dataframe.loc[0, list(ORIGIN_FIELDS)].to_dict()

    def corrupt_then_raise(self, df, row_idx, payload):
        for field_name in ORIGIN_FIELDS:
            df.at[row_idx, field_name] = "secondary overwrite"
        raise RuntimeError("simulated payload failure")

    monkeypatch.setattr(CrossDirectoryEnricherWorker, "_apply_payload_unprotected", corrupt_then_raise)

    with pytest.raises(RuntimeError, match="simulated payload failure"):
        CrossDirectoryEnricherWorker._apply_payload(
            None,
            dataframe,
            0,
            EnrichmentPayload(source_dir="bandcamp", source_url="https://origin-act.bandcamp.com"),
        )

    assert dataframe.loc[0, list(ORIGIN_FIELDS)].to_dict() == before
