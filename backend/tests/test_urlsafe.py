"""SSRF guard for BYOK base URLs (koel.common.urlsafe)."""

from __future__ import annotations

import pytest

from koel.common.urlsafe import UnsafeUrlError, validate_base_url


def test_public_https_is_allowed():
    # No DNS in unit tests: a public literal IP, and hostnames with resolve=False.
    assert validate_base_url("https://8.8.8.8/v1") == "https://8.8.8.8/v1"
    assert validate_base_url("https://api.groq.com/openai/v1", resolve=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.groq.com/v1",  # not https
        "ftp://example.com",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.5/v1",  # private
        "https://192.168.1.10/v1",  # private
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://[::1]/v1",  # loopback v6
        "https://foo.internal/v1",
        "",
    ],
)
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(UnsafeUrlError):
        validate_base_url(url, resolve=False)
