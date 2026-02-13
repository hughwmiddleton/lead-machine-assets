import night_mode_fb


class _FakeAnchor:
    def __init__(self, href: str):
        self._href = href
        self.id = id(self)

    def get_attribute(self, name: str):
        if name == "href":
            return self._href
        return ""


class _FakeContainer:
    def __init__(self, driver):
        self.driver = driver

    def find_elements(self, _by, selector):
        if self.driver.ready and selector.startswith("a"):
            return [_FakeAnchor("https://www.facebook.com/testband")]
        return []


class _FakeDriver:
    def __init__(self):
        self.calls = 0
        self.ready = False

    def find_elements(self, _by, selector):
        self.calls += 1
        if self.calls >= 2:
            self.ready = True
        if selector == 'div[aria-label="Search results"]':
            return [_FakeContainer(self)]
        return []


def test_dom_gate_waits_for_lazy_anchors() -> None:
    driver = _FakeDriver()
    ok = night_mode_fb._wait_for_anchor_population(
        driver,
        'div[aria-label="Search results"]',
        min_anchors=1,
        timeout=1.0,
        poll_seconds=0.01,
    )
    assert ok is True
