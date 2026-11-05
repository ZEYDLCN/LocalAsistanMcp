from __future__ import annotations

import pytest

from localasistan.web.security import LocalOnlyMiddleware, split_host


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("localhost", ("localhost", None)),
        ("LOCALHOST:8765", ("localhost", 8765)),
        ("127.0.0.1:80", ("127.0.0.1", 80)),
        ("[::1]", ("[::1]", None)),
        ("[::1]:8765", ("[::1]", 8765)),
        ("evil.com:abc", ("evil.com:abc", None)),
        ("", ("", None)),
        ("  localhost:1  ", ("localhost", 1)),
        ("[::1", ("[::1", None)),
    ],
)
def test_split_host(raw, expected):
    assert split_host(raw) == expected


@pytest.mark.parametrize(
    ("header", "ok"),
    [
        ("localhost:8765", True),
        ("127.0.0.1:8765", True),
        ("[::1]:8765", True),
        ("localhost", True),  # portsuz (örn. ters vekil) kabul
        ("localhost:9999", False),
        ("localhost.evil.com:8765", False),
        ("evil.com", False),
        ("", False),
        ("127.0.0.1:8765@evil.com", False),
        ("localhost:8765, evil.com", False),
        ("0.0.0.0:8765", False),
    ],
)
def test_host_allow_list(header, ok):
    middleware = LocalOnlyMiddleware(app=None, port=8765)  # type: ignore[arg-type]
    assert middleware._host_ok(header) is ok


def test_extra_hosts_are_opt_in():
    middleware = LocalOnlyMiddleware(app=None, port=8765, extra_hosts=frozenset({"My-Mac.local"}))  # type: ignore[arg-type]
    assert middleware._host_ok("my-mac.local:8765") is True
    assert middleware._host_ok("other.local:8765") is False
