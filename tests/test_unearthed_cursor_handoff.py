from __future__ import annotations

import pipeline_runner


def test_unearthed_profile_batches_chain_forward_in_discovery_order() -> None:
    module = pipeline_runner._load_legacy_module()

    ordered_profile_urls: list[str] = []
    seen_profile_urls: set[str] = set()
    discovered_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
        "https://www.abc.net.au/triplejunearthed/artist/artist-6",
    ]

    for profile_url in discovered_urls:
        module._append_unearthed_profile_url(ordered_profile_urls, seen_profile_urls, profile_url)

    module._append_unearthed_profile_url(
        ordered_profile_urls,
        seen_profile_urls,
        discovered_urls[2],
    )

    first_batch = module._slice_unearthed_profile_urls(ordered_profile_urls, None, 2)
    second_batch = module._slice_unearthed_profile_urls(ordered_profile_urls, first_batch[-1], 2)
    third_batch = module._slice_unearthed_profile_urls(ordered_profile_urls, second_batch[-1], 2)

    assert ordered_profile_urls == discovered_urls
    assert first_batch == discovered_urls[0:2]
    assert second_batch == discovered_urls[2:4]
    assert third_batch == discovered_urls[4:6]


def test_unearthed_remaining_count_uses_latest_cursor_position() -> None:
    module = pipeline_runner._load_legacy_module()

    ordered_profile_urls = [
        "https://www.abc.net.au/triplejunearthed/artist/artist-1",
        "https://www.abc.net.au/triplejunearthed/artist/artist-2",
        "https://www.abc.net.au/triplejunearthed/artist/artist-3",
        "https://www.abc.net.au/triplejunearthed/artist/artist-4",
        "https://www.abc.net.au/triplejunearthed/artist/artist-5",
    ]

    assert module._count_unearthed_remaining_profile_urls(ordered_profile_urls, None) == 5
    assert module._count_unearthed_remaining_profile_urls(ordered_profile_urls, ordered_profile_urls[1]) == 3
    assert module._count_unearthed_remaining_profile_urls(ordered_profile_urls, ordered_profile_urls[3]) == 1
    assert module._count_unearthed_remaining_profile_urls(
        ordered_profile_urls,
        "https://www.abc.net.au/triplejunearthed/artist/missing",
    ) == 0
