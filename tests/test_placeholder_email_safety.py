import pytest

from email_normalizer import (
    filter_obvious_placeholder_emails,
    is_obvious_placeholder_email,
)


@pytest.mark.parametrize(
    "email",
    [
        "user@domain.com",
        "you@example.com",
        "user@example.com",
        "name@example.com",
        "email@example.com",
        "example@example.com",
        "test@example.com",
        "test@test.com",
    ],
)
def test_shared_placeholder_policy_rejects_exact_documentation_addresses(email):
    assert is_obvious_placeholder_email(email)


def test_shared_placeholder_filter_preserves_real_generic_local_parts():
    assert filter_obvious_placeholder_emails(
        [
            "user@artist.test",
            "email@artist.test",
            "test@artist.test",
            "you@example.com",
        ]
    ) == [
        "user@artist.test",
        "email@artist.test",
        "test@artist.test",
    ]
