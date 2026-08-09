"""Security regression tests: outbound delivery gateways.

A FOIA request carries the requester's real identity and reveals what they
are investigating. Anything that silently adds a recipient, or leaves the
document lying around, is a deanonymization risk.
"""

from __future__ import annotations

import asyncio
import os
import stat

import pytest


def _payload(recipient="foia@agency.gov", subject="Records", body="Please send records."):
    from openfoia.gateways.base import DeliveryPayload

    return DeliveryPayload(
        recipient_name="FOIA Officer",
        recipient_address=recipient,
        subject=subject,
        body=body,
    )


def _gateway(**kw):
    from openfoia.gateways.email import EmailGateway

    kw.setdefault("smtp_user", "me@example.com")
    kw.setdefault("from_email", "me@example.com")
    return EmailGateway(**kw)


# ---------------------------------------------------------------------------
# Recipient injection
# ---------------------------------------------------------------------------

MULTI_RECIPIENT = [
    "foia@agency.gov, attacker@evil.example",
    "foia@agency.gov,attacker@evil.example",
    "foia@agency.gov; attacker@evil.example",
    "foia@agency.gov\nBcc: attacker@evil.example",
    "foia@agency.gov\r\nBcc: attacker@evil.example",
]


@pytest.fixture
def captured_smtp(monkeypatch):
    """Capture what would be sent, so no test needs a real SMTP server."""
    from openfoia.gateways import email as email_mod

    captured = {}

    class _SMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg, to_addrs=None):
            captured["to"] = msg["To"]
            captured["to_addrs"] = to_addrs
            captured["sent"] = True

    monkeypatch.setattr(email_mod.smtplib, "SMTP", _SMTP)
    return captured


@pytest.mark.parametrize("recipient", MULTI_RECIPIENT)
def test_send_refuses_multiple_recipients(recipient, captured_smtp):
    """A second address in the To field silently CCs an attacker."""
    from openfoia.gateways.base import DeliveryStatus

    result = asyncio.run(_gateway().send(_payload(recipient=recipient)))

    assert result.status == DeliveryStatus.FAILED
    assert "recipient" in (result.error_message or "").lower()
    assert not captured_smtp.get("sent"), "message was transmitted despite bad recipient"


def test_send_accepts_a_single_plain_address(monkeypatch):
    """The guard must not reject legitimate addresses."""
    from openfoia.gateways import email as email_mod

    sent = {}

    class _SMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg, to_addrs=None):
            sent["to"] = msg["To"]
            sent["to_addrs"] = to_addrs

    monkeypatch.setattr(email_mod.smtplib, "SMTP", _SMTP)

    from openfoia.gateways.base import DeliveryStatus

    result = asyncio.run(_gateway().send(_payload(recipient="foia@agency.gov")))

    assert result.status == DeliveryStatus.SENT
    assert sent["to"] == "foia@agency.gov"


def test_send_passes_explicit_envelope_recipients(monkeypatch):
    """Envelope must be explicit, not re-derived from headers."""
    from openfoia.gateways import email as email_mod

    captured = {}

    class _SMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg, to_addrs=None):
            captured["to_addrs"] = to_addrs

    monkeypatch.setattr(email_mod.smtplib, "SMTP", _SMTP)

    asyncio.run(_gateway().send(_payload(recipient="foia@agency.gov")))

    assert captured["to_addrs"] == ["foia@agency.gov"]


# ---------------------------------------------------------------------------
# Fax media must not sit in a world-readable temp dir
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_fax_media_dir_is_owner_only_and_inside_data_dir(tmp_path, monkeypatch):
    """The staged PDF holds the requester's name, address and subject."""
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    from openfoia.gateways.fax import get_fax_media_dir

    d = get_fax_media_dir()

    assert str(d).startswith(str(tmp_path)), "fax media staged outside the data dir"
    mode = stat.S_IMODE(d.stat().st_mode)
    assert mode & stat.S_IRWXG == 0
    assert mode & stat.S_IRWXO == 0


def test_fax_media_dir_is_covered_by_purge(tmp_path, monkeypatch):
    """Anything under the data dir is shredded by `purge --secure`."""
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path))
    from openfoia.db import get_data_dir
    from openfoia.gateways.fax import get_fax_media_dir

    assert get_data_dir() in get_fax_media_dir().parents or get_fax_media_dir() == get_data_dir()


# ---------------------------------------------------------------------------
# SSRF: file URLs come from third-party API responses
# ---------------------------------------------------------------------------

BAD_URLS = [
    "file:///etc/passwd",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost:8080/admin",
    "gopher://evil.example/x",
    "ftp://evil.example/x",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_records_download_rejects_non_https_or_internal_urls(url):
    from openfoia.records.base import validate_download_url

    with pytest.raises(ValueError):
        validate_download_url(url)


def test_records_download_accepts_expected_https_asset():
    from openfoia.records.base import validate_download_url

    ok = "https://cdn.muckrock.com/foia_files/response.pdf"
    assert validate_download_url(ok) == ok


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://x.test/a/b/report.pdf", "report.pdf"),
        ("https://x.test/a/b/../../etc/passwd", "passwd"),
        ("https://x.test/", "download"),
        ("https://x.test/%2e%2e%2fpasswd", "passwd"),
        ("https://x.test/.hidden", "hidden"),
    ],
)
def test_download_filename_is_sanitized(raw, expected):
    from openfoia.records.base import safe_download_filename

    got = safe_download_filename(raw)

    assert "/" not in got and "\\" not in got
    assert not got.startswith(".")
    assert got == expected
