import pandas as pd

import final_checker


def _run_checked(tmp_path, row):
    input_path = tmp_path / "checker_input.csv"
    pd.DataFrame([row]).to_csv(input_path, index=False)
    return pd.read_csv(final_checker.run_final_checker(str(input_path))).iloc[0]


def test_toledo_production_shape_splits_social_urls_without_false_identity_conflict(tmp_path):
    row = {
        "Artist Name": "TOLEDO",
        "Song Title": "Nothing Yet",
        "Email": "peyton@challengerartists.co",
        "Email_All": "peyton@challengerartists.co",
        "Email_Source_Type": "instagram_enrich",
        "Email_Source_URL": "https://www.instagram.com/toledoband/",
        "Email_Extract_Method": "regex",
        "Spotify_URL": "https://open.spotify.com/artist/2xK3hBpuuHSxmHr96TzgDO",
        "MusicBrainz_MBID": "363a6dcf-7f7d-417e-8191-e15d920cf156",
        "MusicBrainz_Status": "matched",
        "Identity_Match_Method": "spotify_url_relationship",
        "Bandcamp Link": "https://thebandtoledo.bandcamp.com/",
        "SoundCloud Link": "https://soundcloud.com/toledo_music",
        "Website": "https://toledomusic.com/",
        "Social Link": (
            "https://www.instagram.com/toledoband/, "
            "https://www.facebook.com/toledomusicofficial | "
            "https://linktr.ee/toledoband"
        ),
    }

    source = pd.Series(row)
    assert final_checker._extract_instagram_names(source) == ["toledoband"]
    assert final_checker._extract_facebook_names(source) == ["toledomusicofficial"]
    assert "toledomusicofficial http" not in (
        final_checker._extract_instagram_names(source) + final_checker._extract_facebook_names(source)
    )

    checked = _run_checked(tmp_path, row)
    assert checked["name_consistency_flag"] == 1
    assert checked["directory_conflict_flag"] == 0
    assert checked["match_score_overall"] >= 0.75
    assert checked["final_status"] != "BLOCK"


def test_social_tokenizer_does_not_split_delimiter_characters_inside_a_url():
    value = (
        "https://example.com/profile?tags=dream,pop|live, "
        "https://instagram.com/toledoband"
    )

    assert final_checker._split_url_values(value) == [
        "https://example.com/profile?tags=dream,pop|live",
        "https://instagram.com/toledoband",
    ]


def test_multi_social_true_identity_conflict_is_preserved(tmp_path):
    checked = _run_checked(
        tmp_path,
        {
            "Artist Name": "TOLEDO",
            "Song Title": "Nothing Yet",
            "Email": "",
            "Social Link": (
                "https://www.instagram.com/toledoband/ | "
                "https://www.facebook.com/completelyunrelatedartist"
            ),
        },
    )

    assert checked["name_consistency_flag"] == 1
    assert checked["directory_conflict_flag"] == 1
    assert checked["final_status"] == "BLOCK"
