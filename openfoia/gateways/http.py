"""HTTPS calls to delivery providers (Twilio, Lob) through the egress choke point.

The providers' Python SDKs make their own HTTP connections, outside
openfoia.net: they would ignore Tor mode (and honour HTTP(S)_PROXY from the
environment instead). Their REST APIs are small, so the gateways call them
here with egress_client(): DIRECT or Tor per the user's policy, Tor failing
closed (TorUnavailableError, never a direct retry), the non-identifying
User-Agent, and no redirects. POST is never retried by the egress transport,
so a fax or letter is not sent twice.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..net import EgressPolicy, TorUnavailableError, egress_client

API_TIMEOUT = 60.0


class GatewayAPIError(RuntimeError):
    """The provider answered with an error, or with something that is not JSON."""


def tor_unavailable_message(exc: TorUnavailableError) -> str:
    return (
        f"{exc}. Nothing was sent: Tor mode never falls back to a direct connection. "
        "Start Tor, or run again with --direct to send this one request without Tor."
    )


def _error_text(provider: str, response: httpx.Response) -> str:
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
        detail = detail or str(body.get("message") or "")
    detail = " ".join(detail.split())[:300]
    return f"{provider} API error (HTTP {response.status_code})" + (f": {detail}" if detail else "")


def _describe(exc: Exception) -> str:
    """'ReadTimeout' or 'ConnectError: <detail>': some httpx errors stringify to ''."""
    detail = " ".join(str(exc).split())[:200]
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


async def api_request(
    egress: EgressPolicy,
    provider: str,
    method: str,
    url: str,
    *,
    auth: tuple[str, str],
    data: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one request to a provider API and return its JSON object.

    Raises TorUnavailableError (Tor mode without the SOCKS stack) or
    GatewayAPIError: HTTP error status, redirect, a non-JSON body, no
    connection (nothing sent), or no answer after the request went out (it may
    have been accepted; the message says to check before sending again). The
    credentials go in the Authorization header, never in the URL, so they do
    not appear in error messages.
    """
    async with egress_client(egress, timeout=API_TIMEOUT, follow_redirects=False) as client:
        try:
            response = await client.request(method, url, auth=auth, data=data, json=json)
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ProxyError,
            httpx.UnsupportedProtocol,
        ) as exc:
            # No connection to the provider was made, so the request was not sent.
            if not egress.is_tor:
                raise GatewayAPIError(
                    f"could not connect to {provider} ({_describe(exc)}). Nothing was sent"
                ) from exc
            raise GatewayAPIError(
                f"could not reach {provider} through the Tor SOCKS proxy at "
                f"{egress.tor_host}:{egress.tor_port} ({_describe(exc)}). "
                "Nothing was sent directly"
            ) from exc
        except httpx.TransportError as exc:
            # Connected, then lost the answer (read timeout, dropped connection):
            # the provider may already have accepted the request.
            raise GatewayAPIError(
                f"no answer from {provider} after the request was sent ({_describe(exc)}). "
                f"It may have been accepted: check your {provider} dashboard before sending "
                "again, or it may be sent (and charged) twice"
            ) from exc
    if response.is_redirect or response.status_code >= 400:
        raise GatewayAPIError(_error_text(provider, response))
    try:
        body = response.json()
    except ValueError:
        raise GatewayAPIError(f"{provider} answered with a non-JSON body") from None
    if not isinstance(body, dict):
        raise GatewayAPIError(f"{provider} answered with unexpected JSON")
    return body
