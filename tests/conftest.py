"""Shared test fixtures.

The suite is offline by design. OpenFOIA's first principle is that data never
leaves the machine unless the user explicitly chooses, so a test that opens a
real socket is either a bug in the test or a leak in the code — both should
fail loudly rather than hang on a network timeout.
"""

from __future__ import annotations

import socket

import pytest


class NetworkAccessBlocked(RuntimeError):
    """Raised when test code attempts a real outbound connection."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """Block real outbound sockets for every test.

    Opt out with @pytest.mark.allow_network for tests that deliberately
    exercise a live integration (none do by default).
    """
    if request.node.get_closest_marker("allow_network"):
        return

    def _blocked(self, address, *args, **kwargs):
        raise NetworkAccessBlocked(
            f"Test attempted a real network connection to {address!r}. "
            "Mock the transport, or mark the test with @pytest.mark.allow_network."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


@pytest.fixture(autouse=True)
def fast_egress(monkeypatch):
    """Skip real retry backoff and per-host spacing; reset limiter state."""
    from openfoia import net

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(net, "_sleep", _no_sleep)
    monkeypatch.setattr(net, "_next_slot", {})


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network: test may open real outbound network connections"
    )
