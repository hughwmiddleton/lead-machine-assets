import night_mode_fb as nmfb


def _build_enricher(logs):  # noqa: ANN001
    enricher = nmfb.NightModeFacebookEnricher(
        legacy_module=None,
        username="",
        password="",
        logger=lambda msg: logs.append(msg),
        use_shared_session=False,
    )
    enricher._page_budget_remaining = 2
    return enricher


class _FakeBodyElement:
    def __init__(self, text: str):
        self.text = text

    def get_attribute(self, name: str):
        if name == "innerText":
            return self.text
        return ""


class _FbContainerTextDriver:
    def __init__(self, body_text: str, container_texts):  # noqa: ANN001
        self.body_text = body_text
        self.container_texts = list(container_texts or [])

    def find_element(self, _by, value):  # noqa: ANN001
        if value == "body":
            return _FakeBodyElement(self.body_text)
        raise LookupError(value)

    def execute_script(self, script):  # noqa: ANN001
        script_text = str(script or "")
        if "document.body" in script_text:
            return self.body_text
        if "role=\"main\"" in script_text or "role=\"complementary\"" in script_text:
            return list(self.container_texts)
        return []


def test_main_page_html_email_allows_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <head>
                <meta name="description" content="Bookings: bookings@artist.com" />
              </head>
              <body>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>About: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"bookings@artist.com", "about@artist.com"}
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert set((night_result.email_all or "").split(";")) == {"bookings@artist.com", "about@artist.com"}
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, about_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] Scanning main page HTML for emails" in msg for msg in logs)
    assert any("[FB Email] Found email on main page: bookings@artist.com" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)
    assert not any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


def test_main_page_script_email_allows_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No visible email on this page.</div>
                <script type="application/json">
                  {"email":"booking@artist.com"}
                </script>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>About: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"booking@artist.com", "about@artist.com"}
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert set((night_result.email_all or "").split(";")) == {"booking@artist.com", "about@artist.com"}
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, about_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] Scanning main page HTML for emails" in msg for msg in logs)
    assert any("[FB Email] Found email on main page: booking@artist.com" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)
    assert not any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


def test_main_page_rendered_visible_text_email_allows_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No visible email on this page.</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
            "Intro Contact brighteyedbookings@gmail.com",
        ),
        about_url: (
            """
            <html>
              <body>
                <div>About: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
            "",
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        html, resolved, visible_text = pages[url]
        enricher._last_fb_visible_text = visible_text
        return html, resolved

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"brighteyedbookings@gmail.com", "about@artist.com"}
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert set((night_result.email_all or "").split(";")) == {"brighteyedbookings@gmail.com", "about@artist.com"}
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, about_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] Found email on main page: brighteyedbookings@gmail.com" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)
    assert not any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


def test_main_page_rendered_obfuscated_visible_text_email_allows_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No visible email on this page.</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
            "Bookings name [at] artist [dot] com",
        ),
        about_url: (
            """
            <html>
              <body>
                <div>About: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
            "",
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        html, resolved, visible_text = pages[url]
        enricher._last_fb_visible_text = visible_text
        return html, resolved

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"name@artist.com", "about@artist.com"}
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert set((night_result.email_all or "").split(";")) == {"name@artist.com", "about@artist.com"}
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, about_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] Found email on main page: name@artist.com" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)
    assert not any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


def test_main_page_fb_container_fallback_email_allows_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No visible email on this page.</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
            "No email on main page",
            ["Intro Contact brighteyedbookings@gmail.com"],
        ),
        about_url: (
            """
            <html>
              <body>
                <div>About: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
            "",
            [],
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        html, resolved, body_text, container_texts = pages[url]
        driver = _FbContainerTextDriver(body_text, container_texts)
        enricher._last_fb_visible_text = nmfb._extract_fb_visible_text_with_container_fallback(driver)
        return html, resolved

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"brighteyedbookings@gmail.com", "about@artist.com"}
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert set((night_result.email_all or "").split(";")) == {"brighteyedbookings@gmail.com", "about@artist.com"}
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, about_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] Found email on main page: brighteyedbookings@gmail.com" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)
    assert not any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


def test_main_page_live_anchor_mailto_allows_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No visible email on this page.</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
            ["mailto:info@artist.com"],
        ),
        about_url: (
            """
            <html>
              <body>
                <div>About: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
            [],
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        html, resolved, anchor_values = pages[url]
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = list(anchor_values)
        return html, resolved

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"info@artist.com", "about@artist.com"}
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert set((night_result.email_all or "").split(";")) == {"info@artist.com", "about@artist.com"}
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, about_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)
    assert not any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


def test_discovery_main_page_html_email_uses_light_fetch_and_keeps_about_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    surface_calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <head>
                <meta name="description" content="Bookings: bookings@artist.com" />
              </head>
              <body>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>About: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
        ),
    }

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append((url, collect_surfaces))
        html, resolved = pages[url]
        if not collect_surfaces:
            enricher._last_fb_visible_text = ""
            enricher._last_fb_live_anchor_values = []
            enricher._last_fb_reveal_actions = []
        return html, resolved

    def fake_collect(driver_kind="session"):  # noqa: ANN001
        surface_calls.append(driver_kind)
        return "", "", [], []

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(enricher, "_collect_current_fb_email_surface_state", fake_collect)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={"url": main_url, "base_score": 1.2, "match_level": "near", "search_discovery_accepted": True},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"bookings@artist.com", "about@artist.com"}
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [(main_url, False), (about_url, True)]
    assert surface_calls == []
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_discovery_main_page_rendered_text_email_escalates_after_light_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    surface_calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No visible email on this page.</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>About: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
        ),
    }

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append((url, collect_surfaces))
        html, resolved = pages[url]
        if not collect_surfaces:
            enricher._last_fb_visible_text = ""
            enricher._last_fb_live_anchor_values = []
            enricher._last_fb_reveal_actions = []
        return html, resolved

    def fake_collect(driver_kind="session"):  # noqa: ANN001
        surface_calls.append(driver_kind)
        return pages[main_url][0], "Intro Contact brighteyedbookings@gmail.com", [], []

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(enricher, "_collect_current_fb_email_surface_state", fake_collect)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={"url": main_url, "base_score": 1.2, "match_level": "near", "search_discovery_accepted": True},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"brighteyedbookings@gmail.com", "about@artist.com"}
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [(main_url, False), (about_url, True)]
    assert surface_calls == ["session"]
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_discovery_main_page_live_anchor_mailto_escalates_after_light_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    surface_calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No visible email on this page.</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>About: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
        ),
    }

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append((url, collect_surfaces))
        html, resolved = pages[url]
        if not collect_surfaces:
            enricher._last_fb_visible_text = ""
            enricher._last_fb_live_anchor_values = []
            enricher._last_fb_reveal_actions = []
        return html, resolved

    def fake_collect(driver_kind="session"):  # noqa: ANN001
        surface_calls.append(driver_kind)
        return pages[main_url][0], "", ["mailto:info@artist.com"], []

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(enricher, "_collect_current_fb_email_surface_state", fake_collect)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={"url": main_url, "base_score": 1.2, "match_level": "near", "search_discovery_accepted": True},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"info@artist.com", "about@artist.com"}
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [(main_url, False), (about_url, True)]
    assert surface_calls == ["session"]
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_explicit_pass_a_main_page_email_skips_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    extract_calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>Bookings: bookings@artist.com</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
    }

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append((url, collect_surfaces))
        return pages[url]

    def fake_extract(sample: str):  # noqa: ANN001
        extract_calls.append(sample)
        if len(extract_calls) == 1:
            return ["bookings@artist.com"]
        raise AssertionError("explicit PASS A main-page scan should stop after first usable email")

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_extract_fb_emails_from_text_sample", fake_extract)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={"explicit_accepted_url": True},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.email == "bookings@artist.com"
    assert night_result.about_attempted == "no"
    assert night_result.about_result == ""
    assert calls == [(main_url, True)]
    assert all(url != about_url for url, _collect_surfaces in calls)
    assert len(extract_calls) == 1
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert not any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)


def test_secondary_fetch_still_runs_when_main_page_has_no_email(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>Bookings: bookings@artist.com</div>
              </body>
            </html>
            """,
            about_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, about_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] No email found on main page; evaluating contact/about fetch" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)


def test_secondary_fetch_still_runs_when_main_page_only_has_low_quality_email(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>Contact: to@nomograph.mastering</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>Bookings: bookings@artist.com</div>
              </body>
            </html>
            """,
            about_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.email == "bookings@artist.com"
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, about_url]
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] No email found on main page; evaluating contact/about fetch" in msg for msg in logs)


def test_secondary_fetch_prefers_about_contact_and_basic_info_and_stays_within_cap(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"
    contact_info_url = "https://www.facebook.com/artist/about_contact_and_basic_info"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
                <a href="/artist/about">About</a>
                <a href="/artist/about_contact_and_basic_info">Contact info</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>No email here.</div>
              </body>
            </html>
            """,
            about_url,
        ),
        contact_info_url: (
            """
            <html>
              <body>
                <div>Bookings: bookings@artist.com</div>
              </body>
            </html>
            """,
            contact_info_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, contact_info_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] No email found on main page; evaluating contact/about fetch" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {contact_info_url}" in msg for msg in logs)


def test_secondary_fetch_prefers_contact_and_basic_info_over_about_and_stays_within_cap(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"
    contact_info_url = "https://www.facebook.com/artist/contact_and_basic_info"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
                <a href="/artist/about">About</a>
                <a href="/artist/contact_and_basic_info">Contact info</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>No email here.</div>
              </body>
            </html>
            """,
            about_url,
        ),
        contact_info_url: (
            """
            <html>
              <body>
                <div>Bookings: bookings@artist.com</div>
              </body>
            </html>
            """,
            contact_info_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, contact_info_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] No email found on main page; evaluating contact/about fetch" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {contact_info_url}" in msg for msg in logs)


def test_profile_secondary_fetch_prefers_about_details_over_about_and_stays_within_cap(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/profile.php?id=123"
    about_url = "https://www.facebook.com/profile.php?id=123&sk=about"
    details_url = "https://www.facebook.com/profile.php?id=123&sk=about_details"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
                <a href="/profile.php?id=123&sk=about">About</a>
                <a href="/profile.php?id=123&sk=about_details">Details</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>No email here.</div>
              </body>
            </html>
            """,
            about_url,
        ),
        details_url: (
            """
            <html>
              <body>
                <div>Bookings: bookings@artist.com</div>
              </body>
            </html>
            """,
            details_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, details_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] No email found on main page; evaluating contact/about fetch" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {details_url}" in msg for msg in logs)


def test_direct_about_fallback_runs_when_no_contact_link_detected(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    fallback_url = "https://www.facebook.com/artist/about_contact_and_basic_info"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
              </body>
            </html>
            """,
            main_url,
        ),
        fallback_url: (
            """
            <html>
              <body>
                <div>Bookings: bookings@artist.com</div>
              </body>
            </html>
            """,
            fallback_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, fallback_url]
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("trying direct fallback" in msg for msg in logs)


def test_candidate_fetch_budget_remains_capped_at_two_pages(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>No email here either.</div>
              </body>
            </html>
            """,
            about_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == []
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "no_email"
    assert calls == [main_url, about_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "no_email_on_page"
    assert any("[FB Email] No email found on main page; evaluating contact/about fetch" in msg for msg in logs)


def test_secondary_fetch_uses_direct_about_fallback_when_only_invalid_internal_surfaces_exist(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    fallback_url = "https://www.facebook.com/artist/about_contact_and_basic_info"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
                <a href="/events/birthdays/?foo=1">About</a>
                <a href="/artist/videos">Contact</a>
              </body>
            </html>
            """,
            main_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == []
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "no_email"
    assert calls == [main_url, fallback_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "no_email_on_page"
    assert any("[FB Email] No email found on main page; evaluating contact/about fetch" in msg for msg in logs)
    assert any("trying direct fallback" in msg for msg in logs)


def test_secondary_fetch_rejects_redirected_posts_final_landing(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/wetdenim"
    about_url = "https://www.facebook.com/wetdenim/about"
    posts_url = "https://www.facebook.com/charlotte.holding/posts/123"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>No email on the main page.</div>
                <a href="/wetdenim/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
        about_url: (
            """
            <html>
              <body>
                <div>Bookings: bookings@wrong-page.com</div>
              </body>
            </html>
            """,
            posts_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Wet Denim", "Email_All": ""},
        "Wet Denim",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == []
    assert night_result is not None
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "invalid_destination"
    assert calls == [main_url, about_url]
    assert len(calls) == 2
    assert driver_kind == "session"
    assert outcome == "no_email_on_page"
    assert any("Ignoring About/contact result due to entity/surface mismatch" in msg for msg in logs)


def test_explicit_seed_url_keeps_main_page_email_without_candidate_context(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/exactartist"
    contact_info_url = "https://www.facebook.com/exactartist/about_contact_and_basic_info"

    pages = {
        main_url: (
            """
            <html>
              <head>
                <title>Exact Artist</title>
              </head>
              <body>
                <div>Bookings: bookings@artist.com</div>
              </body>
            </html>
            """,
            main_url,
        ),
        contact_info_url: (
            """
            <html>
              <body>
                <div>No email on the about page.</div>
              </body>
            </html>
            """,
            contact_info_url,
        ),
    }

    def fake_fetch(url, goto_about=False):  # noqa: ANN001
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {
            "Artist Name": "Exact Artist",
            "Email_All": "",
            "Facebook_URL": main_url,
        },
        "Exact Artist",
        allow_anon=False,
        candidate_context={},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.accepted is True
    assert night_result.email == "bookings@artist.com"
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "no_email"
    assert calls == [main_url, contact_info_url]
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_search_candidate_still_rejects_extracted_email_without_music_signals(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    main_url = "https://www.facebook.com/searchcandidate"

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        assert url == main_url
        assert collect_surfaces is True
        return (
            """
            <html>
              <head>
                <title>Wrong Page</title>
              </head>
              <body>
                <div>Bookings: rejectme@artist.com</div>
              </body>
            </html>
            """,
            main_url,
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(
        nmfb,
        "should_accept_email_override",
        lambda *args, **kwargs: (False, "email_override_reject:name_mismatch"),
    )

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {
            "Artist Name": "Exact Artist",
            "Email_All": "",
        },
        "Exact Artist",
        allow_anon=False,
        candidate_context={"url": main_url, "base_score": 0.2, "match_level": "near"},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == []
    assert night_result is not None
    assert night_result.accepted is False
    assert night_result.reject_reason == "email_override_reject:name_mismatch"
    assert night_result.email is None
    assert night_result.email_all == ""
    assert driver_kind == "session"
    assert outcome == "no_email_on_page"
