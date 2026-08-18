"""C2 — the process must refuse any non-loopback bind. No override exists."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from .conftest import make_settings


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "127.0.0.53"])
def test_loopback_binds_accepted(host: str):
    assert make_settings(bind_host=host).bind_host == host


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.5", "100.64.0.1", "localhost", "", "example.com"],
)
def test_non_loopback_binds_refused(host: str):
    with pytest.raises(ValidationError):
        make_settings(bind_host=host)
