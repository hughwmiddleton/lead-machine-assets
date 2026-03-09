import pytest

pytest.importorskip("PyQt5")

import cross_directory_enricher as cde


@pytest.mark.parametrize(
    ("email", "expected_role"),
    [
        ("mgmt@x.com", "management"),
        ("bookings@x.com", "booking"),
        ("press@x.com", "press"),
        ("info@x.com", "general"),
        ("random@x.com", None),
    ],
)
def test_classify_contact_role_from_email(email, expected_role):
    assert cde._classify_contact_role_from_email(email) == expected_role

