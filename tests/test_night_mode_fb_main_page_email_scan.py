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


def test_discovery_main_page_html_email_uses_first_pass_surfaces_and_skips_recollect(monkeypatch) -> None:
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
    assert emails == ["bookings@artist.com"]
    assert night_result is not None
    assert night_result.email == "bookings@artist.com"
    assert night_result.about_attempted == "no"
    assert night_result.about_result == ""
    assert calls == [(main_url, True)]
    assert surface_calls == []
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_discovery_main_page_rendered_text_email_uses_first_pass_surfaces(monkeypatch) -> None:
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
        enricher._last_fb_visible_text = "Intro Contact brighteyedbookings@gmail.com"
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
    assert emails == ["brighteyedbookings@gmail.com"]
    assert night_result is not None
    assert night_result.email == "brighteyedbookings@gmail.com"
    assert night_result.about_attempted == "no"
    assert night_result.about_result == ""
    assert calls == [(main_url, True)]
    assert surface_calls == []
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_discovery_main_page_live_anchor_mailto_uses_first_pass_surfaces(monkeypatch) -> None:
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
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = ["mailto:info@artist.com"]
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
    assert emails == ["info@artist.com"]
    assert night_result is not None
    assert night_result.email == "info@artist.com"
    assert night_result.about_attempted == "no"
    assert night_result.about_result == ""
    assert calls == [(main_url, True)]
    assert surface_calls == []
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_discovery_candidate_uses_shared_accepted_page_sweep(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"
    observed = {}

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append((url, collect_surfaces))
        return "<html><body><a href='/artist/about'>About</a></body></html>", main_url

    def fake_shared_sweep(fb_url, fetch_surface, **kwargs):  # noqa: ANN001
        observed["fb_url"] = fb_url
        observed["continue_after_main_email"] = kwargs.get("continue_after_main_email")
        observed["stop_after_first_filtered"] = kwargs.get("stop_after_first_filtered")
        observed["refresh_main_surface"] = kwargs.get("refresh_main_surface")
        return nmfb.FacebookAcceptedPageSweepResult(
            main_surface=nmfb.FacebookAcceptedPageFetchResult(
                requested_url=fb_url,
                resolved_url=main_url,
                html="<html><body><a href='/artist/about'>About</a></body></html>",
            ),
            main_emails=["info@artist.com"],
            combined_emails=["info@artist.com"],
            final_resolved_url=main_url,
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_run_bounded_fb_accepted_page_sweep", fake_shared_sweep)
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
    assert observed["fb_url"] == main_url
    assert observed["continue_after_main_email"] is False
    assert observed["stop_after_first_filtered"] is False
    assert observed["refresh_main_surface"] is None
    assert calls == [(main_url, True)]
    assert emails == ["info@artist.com"]
    assert night_result is not None
    assert night_result.email == "info@artist.com"
    assert night_result.about_attempted == "no"
    assert night_result.about_result == ""
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_discovery_candidate_about_fetch_remains_reachable_without_main_refresh(monkeypatch) -> None:
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
                <div>Bookings: about@artist.com</div>
              </body>
            </html>
            """,
            about_url,
        ),
    }

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append((url, collect_surfaces))
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = []
        enricher._last_fb_reveal_actions = []
        return pages[url]

    def fake_collect(driver_kind="session"):  # noqa: ANN001
        surface_calls.append(driver_kind)
        raise AssertionError("discovery accepted pages should not recollect the main surface before about/contact fallback")

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
    assert emails == ["about@artist.com"]
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [(main_url, True), (about_url, True)]
    assert surface_calls == []
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)


def test_discovery_promoted_candidate_uses_fast_loader(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    main_url = "https://www.facebook.com/artist"
    calls = []
    observed = {}

    def fake_fetch_anon(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append(("anon", url, collect_surfaces))
        enricher._last_fb_visible_text = "Bookings: fast@artist.com"
        enricher._last_fb_live_anchor_values = []
        enricher._last_fb_reveal_actions = []
        return "<html><body>Bookings: fast@artist.com</body></html>", url

    def fail_session(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("promoted PASS B candidates should stay on the fast accepted-page loader")

    def fake_shared_sweep(fb_url, fetch_surface, **kwargs):  # noqa: ANN001
        observed["fb_url"] = fb_url
        observed["continue_after_main_email"] = kwargs.get("continue_after_main_email")
        observed["stop_after_first_filtered"] = kwargs.get("stop_after_first_filtered")
        fetch_result = fetch_surface(fb_url)
        return nmfb.FacebookAcceptedPageSweepResult(
            main_surface=nmfb.FacebookAcceptedPageFetchResult(
                requested_url=fb_url,
                resolved_url=fetch_result.resolved_url,
                html=fetch_result.html,
                rendered_text="Bookings: fast@artist.com",
            ),
            main_emails=["fast@artist.com"],
            combined_emails=["fast@artist.com"],
            final_resolved_url=fetch_result.resolved_url,
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url_anon", fake_fetch_anon)
    monkeypatch.setattr(enricher, "_fetch_html_with_url", fail_session)
    monkeypatch.setattr(nmfb, "_run_bounded_fb_accepted_page_sweep", fake_shared_sweep)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=True,
        candidate_context={
            "url": main_url,
            "base_score": 1.2,
            "match_level": "near",
            "search_discovery_accepted": True,
            "explicit_accepted_url": True,
            "accepted_page_fast_loader_safe": True,
        },
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["fast@artist.com"]
    assert night_result is not None
    assert night_result.email == "fast@artist.com"
    assert night_result.about_attempted == "no"
    assert night_result.about_result == ""
    assert observed["fb_url"] == main_url
    assert observed["continue_after_main_email"] is False
    assert observed["stop_after_first_filtered"] is True
    assert calls == [("anon", main_url, True)]
    assert driver_kind == "anon_fast"
    assert outcome == "found_email"
    assert any("[Night FB][AcceptedPage] using fast loader" in msg for msg in logs)


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


def test_explicit_pass_a_uses_shared_accepted_page_sweep(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    main_url = "https://www.facebook.com/artist"
    observed = {}

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        return "<html><body>Bookings: explicit@example.com</body></html>", main_url

    def fake_shared_sweep(fb_url, fetch_surface, **kwargs):  # noqa: ANN001
        observed["fb_url"] = fb_url
        observed["continue_after_main_email"] = kwargs.get("continue_after_main_email")
        observed["stop_after_first_filtered"] = kwargs.get("stop_after_first_filtered")
        return nmfb.FacebookAcceptedPageSweepResult(
            main_surface=nmfb.FacebookAcceptedPageFetchResult(
                requested_url=fb_url,
                resolved_url=main_url,
                html="<html><body>Bookings: explicit@example.com</body></html>",
                rendered_text="Bookings: explicit@example.com",
            ),
            main_emails=["explicit@example.com"],
            combined_emails=["explicit@example.com"],
            final_resolved_url=main_url,
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_run_bounded_fb_accepted_page_sweep", fake_shared_sweep)

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={"explicit_accepted_url": True},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert observed["fb_url"] == main_url
    assert observed["continue_after_main_email"] is False
    assert observed["stop_after_first_filtered"] is True
    assert emails == ["explicit@example.com"]
    assert night_result is not None
    assert night_result.email == "explicit@example.com"
    assert night_result.about_attempted == "no"
    assert night_result.about_result == ""
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_explicit_pass_a_accepted_page_sweep_keeps_mailto_and_visible_main_emails() -> None:
    main_url = "https://www.facebook.com/artist"
    calls = []

    def fake_fetch_surface(url):  # noqa: ANN001
        calls.append(url)
        return nmfb.FacebookAcceptedPageFetchResult(
            requested_url=url,
            resolved_url=url,
            html='<html><body><a href="mailto:hidden@artist.com">Email</a></body></html>',
            rendered_text="Intro visible@artist.com",
            anchor_values=["mailto:hidden@artist.com"],
        )

    result = nmfb._run_bounded_fb_accepted_page_sweep(
        main_url,
        fake_fetch_surface,
        continue_after_main_email=False,
        stop_after_first_filtered=True,
    )

    assert calls == [main_url]
    assert result.main_emails == ["hidden@artist.com", "visible@artist.com"]
    assert result.combined_emails == ["hidden@artist.com", "visible@artist.com"]
    assert result.main_mailto is True
    assert result.secondary_attempted is False
    assert result.secondary_surface is None


def test_explicit_pass_a_shared_sweep_preserves_all_main_candidates_downstream(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    main_url = "https://www.facebook.com/artist"

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        return "<html><body><div>No visible email in raw HTML.</div></body></html>", main_url

    def fake_shared_sweep(fb_url, fetch_surface, **kwargs):  # noqa: ANN001
        return nmfb.FacebookAcceptedPageSweepResult(
            main_surface=nmfb.FacebookAcceptedPageFetchResult(
                requested_url=fb_url,
                resolved_url=main_url,
                html='<html><body><a href="mailto:hidden@artist.com">Email</a></body></html>',
                rendered_text="Intro visible@artist.com",
                anchor_values=["mailto:hidden@artist.com"],
            ),
            main_emails=["hidden@artist.com", "visible@artist.com"],
            combined_emails=["hidden@artist.com", "visible@artist.com"],
            main_mailto=True,
            final_resolved_url=main_url,
        )

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_run_bounded_fb_accepted_page_sweep", fake_shared_sweep)

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={"explicit_accepted_url": True},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert set(emails) == {"hidden@artist.com", "visible@artist.com"}
    assert night_result is not None
    assert set((night_result.email_all or "").split(";")) == {"hidden@artist.com", "visible@artist.com"}
    assert night_result.about_attempted == "no"
    assert night_result.about_result == ""
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_explicit_pass_a_safe_fast_loader_respects_allow_anon_gate(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    main_url = "https://www.facebook.com/artist"
    about_url = "https://www.facebook.com/artist/about"
    calls = []

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
            "",
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
            "About: about@artist.com",
        ),
    }

    def fail_anon(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("anon fast path should not run when allow_anon is false")

    def fake_fetch_session(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append(("session", url, collect_surfaces))
        html, resolved, visible_text = pages[url]
        enricher._last_fb_visible_text = visible_text
        enricher._last_fb_live_anchor_values = []
        return html, resolved

    monkeypatch.setattr(enricher, "_fetch_html_with_url_anon", fail_anon)
    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch_session)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={
            "explicit_accepted_url": True,
            "accepted_page_fast_loader_safe": True,
        },
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["about@artist.com"]
    assert night_result is not None
    assert night_result.email == "about@artist.com"
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [
        ("session", main_url, True),
        ("session", about_url, True),
    ]
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)
    assert not any("anon_fast_path" in msg for msg in logs)


def test_explicit_pass_a_session_failure_still_allows_anon_fallback(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    main_url = "https://www.facebook.com/artist"
    calls = []

    class _SessionUnusable:
        last_health_ok = False
        last_health_reason = "checkpoint"

    enricher.session = _SessionUnusable()

    def fake_fetch_anon(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append(("anon", url, collect_surfaces))
        enricher._last_fb_visible_text = "Bookings: anon@example.com"
        enricher._last_fb_live_anchor_values = []
        return "<html><body>Bookings: anon@example.com</body></html>", url

    def fake_fetch_session(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append(("session", url, collect_surfaces))
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = []
        return None, "https://www.facebook.com/checkpoint/"

    monkeypatch.setattr(enricher, "_fetch_html_with_url_anon", fake_fetch_anon)
    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch_session)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=True,
        candidate_context={
            "explicit_accepted_url": True,
            "accepted_page_fast_loader_safe": False,
        },
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["anon@example.com"]
    assert night_result is not None
    assert night_result.email == "anon@example.com"
    assert night_result.about_attempted == "no"
    assert calls == [
        ("session", main_url, True),
        ("anon", main_url, True),
    ]
    assert driver_kind == "anon_fallback"
    assert outcome == "found_email"
    assert any("anon_after_session_fail" in msg for msg in logs)


def test_explicit_pass_a_budget_exhaustion_does_not_escalate_to_anon(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    main_url = "https://www.facebook.com/artist"
    calls = []

    def fail_anon(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("anon fallback should not run for budget exhaustion")

    def fake_fetch_session(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append(("session", url, collect_surfaces))
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = []
        return None, None

    monkeypatch.setattr(enricher, "_fetch_html_with_url_anon", fail_anon)
    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch_session)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=True,
        candidate_context={
            "explicit_accepted_url": True,
            "accepted_page_fast_loader_safe": False,
        },
    )

    assert result is None
    assert calls == [("session", main_url, True)]
    assert not any("anon_after_session_fail" in msg for msg in logs)


def test_explicit_pass_a_empty_session_fetch_does_not_escalate_to_anon(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    main_url = "https://www.facebook.com/artist"
    calls = []

    def fail_anon(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("anon fallback should not run for non-auth empty fetches")

    def fake_fetch_session(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append(("session", url, collect_surfaces))
        enricher._last_fb_timeout = True
        enricher._last_fb_timeout_url = url
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = []
        return None, url

    monkeypatch.setattr(enricher, "_fetch_html_with_url_anon", fail_anon)
    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch_session)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=True,
        candidate_context={
            "explicit_accepted_url": True,
            "accepted_page_fast_loader_safe": False,
        },
    )

    assert result == (None, [], "session", "timeout")
    assert calls == [("session", main_url, True)]
    assert not any("anon_after_session_fail" in msg for msg in logs)


def test_explicit_pass_a_weak_main_capture_recollects_once_and_skips_about(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    enricher.session = type("SessionStub", (), {"driver": object()})()
    calls = []
    surface_calls = []
    main_url = "https://www.facebook.com/tbish"
    about_url = "https://www.facebook.com/tbish/about"
    main_html = """
        <html>
          <body>
            <div>Intro</div>
            <a href="/tbish/about">About</a>
          </body>
        </html>
    """

    pages = {
        main_url: (
            main_html,
            main_url,
        ),
    }

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append((url, collect_surfaces))
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = []
        return pages[url]

    def fake_collect(driver_kind="session"):  # noqa: ANN001
        surface_calls.append(driver_kind)
        return main_html, "Intro zac@altchord.com", [], []

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(enricher, "_collect_current_fb_email_surface_state", fake_collect)
    monkeypatch.setattr(nmfb.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "tbish", "Email_All": ""},
        "tbish",
        allow_anon=False,
        candidate_context={"explicit_accepted_url": True},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["zac@altchord.com"]
    assert night_result is not None
    assert night_result.email == "zac@altchord.com"
    assert night_result.about_attempted == "no"
    assert night_result.about_result == ""
    assert calls == [(main_url, True)]
    assert all(url != about_url for url, _collect_surfaces in calls)
    assert surface_calls == ["session"]
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[Night FB][Explicit Stabilize] weak main-page capture detected" in msg for msg in logs)
    assert not any(f"[FB Email] Visiting {about_url}" in msg for msg in logs)


def test_explicit_pass_a_meaningful_empty_main_page_skips_recollect_and_uses_fallback(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    surface_calls = []
    main_url = "https://www.facebook.com/artist"
    fallback_url = "https://www.facebook.com/artist/directory_contact_info"

    pages = {
        main_url: (
            """
            <html>
              <body>
                <div>This page is live, but the main page does not list an email address.</div>
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
        enricher._last_fb_visible_text = ""
        enricher._last_fb_live_anchor_values = []
        return pages[url]

    def fake_collect(driver_kind="session"):  # noqa: ANN001
        surface_calls.append(driver_kind)
        raise AssertionError("explicit PASS A should not recollect when the first main-page capture is meaningfully rendered")

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(enricher, "_collect_current_fb_email_surface_state", fake_collect)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

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
    assert night_result.about_attempted == "yes"
    assert night_result.about_result == "emails_found"
    assert calls == [main_url, fallback_url]
    assert surface_calls == []
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert not any("[Night FB][Explicit Stabilize] weak main-page capture detected" in msg for msg in logs)
    assert any(f"[FB Email] Visiting {fallback_url}" in msg for msg in logs)


def test_explicit_pass_a_session_fetch_skips_pre_nav_session_validation(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"

    def fake_fetch(url, goto_about=False, collect_surfaces=True, skip_pre_nav_session_validation=False):  # noqa: ANN001
        calls.append((url, collect_surfaces, skip_pre_nav_session_validation))
        enricher._last_fb_visible_text = "Bookings: accepted@artist.com"
        enricher._last_fb_live_anchor_values = []
        return "<html><body>Bookings: accepted@artist.com</body></html>", url

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(nmfb, "_night_fb_has_music_signals", lambda soup, context: False)
    monkeypatch.setattr(nmfb, "should_accept_email_override", lambda *args, **kwargs: (True, "test_override"))

    result = enricher._scrape_single_fb_candidate(
        main_url,
        {"Artist Name": "Artist", "Email_All": ""},
        "Artist",
        allow_anon=False,
        candidate_context={"explicit_accepted_url": True},
    )

    assert result is not None
    night_result, emails, driver_kind, outcome = result
    assert emails == ["accepted@artist.com"]
    assert night_result is not None
    assert night_result.email == "accepted@artist.com"
    assert calls == [(main_url, True, True)]
    assert driver_kind == "session"
    assert outcome == "found_email"


def test_explicit_pass_a_good_visible_text_skips_recollect(monkeypatch) -> None:
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
                <div>No email in HTML.</div>
                <a href="/artist/about">About</a>
              </body>
            </html>
            """,
            main_url,
        ),
    }

    def fake_fetch(url, goto_about=False, collect_surfaces=True):  # noqa: ANN001
        calls.append((url, collect_surfaces))
        enricher._last_fb_visible_text = "Intro bookings@artist.com"
        enricher._last_fb_live_anchor_values = []
        return pages[url]

    def fake_collect(driver_kind="session"):  # noqa: ANN001
        surface_calls.append(driver_kind)
        raise AssertionError("explicit PASS A should not recollect when the first main-page capture already has visible text")

    monkeypatch.setattr(enricher, "_fetch_html_with_url", fake_fetch)
    monkeypatch.setattr(enricher, "_collect_current_fb_email_surface_state", fake_collect)
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
    assert surface_calls == []
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert not any("[Night FB][Explicit Stabilize] weak main-page capture detected" in msg for msg in logs)
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
    fallback_url = "https://www.facebook.com/artist/directory_contact_info"

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
    fallback_url = "https://www.facebook.com/artist/directory_contact_info"

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


def test_explicit_seed_url_keeps_main_page_email_without_candidate_context(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/exactartist"
    contact_info_url = "https://www.facebook.com/exactartist/directory_contact_info"

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
