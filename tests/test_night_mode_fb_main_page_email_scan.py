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


def test_main_page_html_email_stops_before_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"

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
    assert night_result.about_attempted == "no"
    assert calls == [main_url]
    assert len(calls) == 1
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] Scanning main page HTML for emails" in msg for msg in logs)
    assert any("[FB Email] Found email on main page: bookings@artist.com" in msg for msg in logs)
    assert any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


def test_main_page_script_email_stops_before_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"

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
    assert emails == ["booking@artist.com"]
    assert night_result is not None
    assert night_result.email == "booking@artist.com"
    assert night_result.about_attempted == "no"
    assert calls == [main_url]
    assert len(calls) == 1
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] Scanning main page HTML for emails" in msg for msg in logs)
    assert any("[FB Email] Found email on main page: booking@artist.com" in msg for msg in logs)
    assert any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


def test_main_page_rendered_visible_text_email_stops_before_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"

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
    assert emails == ["brighteyedbookings@gmail.com"]
    assert night_result is not None
    assert night_result.email == "brighteyedbookings@gmail.com"
    assert night_result.about_attempted == "no"
    assert calls == [main_url]
    assert len(calls) == 1
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] Found email on main page: brighteyedbookings@gmail.com" in msg for msg in logs)
    assert any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


def test_main_page_fb_container_fallback_email_stops_before_secondary_fetch(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"

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
    assert emails == ["brighteyedbookings@gmail.com"]
    assert night_result is not None
    assert night_result.email == "brighteyedbookings@gmail.com"
    assert night_result.about_attempted == "no"
    assert calls == [main_url]
    assert len(calls) == 1
    assert driver_kind == "session"
    assert outcome == "found_email"
    assert any("[FB Email] Found email on main page: brighteyedbookings@gmail.com" in msg for msg in logs)
    assert any("[FB Email] Skipping contact/about fetch because main page email already found" in msg for msg in logs)


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


def test_secondary_fetch_is_skipped_when_only_invalid_internal_surfaces_exist(monkeypatch) -> None:
    logs = []
    enricher = _build_enricher(logs)
    calls = []
    main_url = "https://www.facebook.com/artist"

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
    assert night_result.about_attempted == "no"
    assert night_result.about_result == "no_contact_link"
    assert calls == [main_url]
    assert len(calls) == 1
    assert driver_kind == "session"
    assert outcome == "no_email_on_page"
    assert any("[FB Email] No email found on main page; evaluating contact/about fetch" in msg for msg in logs)
    assert any("[FB Email] No valid contact surface found" in msg for msg in logs)
