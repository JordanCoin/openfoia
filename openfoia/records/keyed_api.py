"""Shared pieces for records APIs that need the user's personal key.

Used by the OCCRP Aleph and UK Companies House adapters:

- :class:`RequestBudget`: a local count of requests in rolling windows, kept in
  the data directory, so a batch waits or stops before the provider's limit
  instead of running into HTTP 429s that can get a key banned.
- :func:`read_capped`: one GET through ``egress_client`` that never follows a
  redirect (a redirect would carry the key to a second, unchecked URL) and
  stops reading a body over a size cap.
- :func:`clean_line` / :func:`clean_text`: upstream strings made safe to print
  and store (see :mod:`openfoia.termsafe`).

No network code runs at import time and nothing here reads a key: each adapter
reads its own from the environment and passes it through
``RecordAdapter._personal_credential`` so it is withheld over Tor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ..db import get_data_dir
from ..net import EgressPolicy, _retry_after, egress_client
from ..termsafe import safe_text
from .base import AdapterRequestError

logger = logging.getLogger(__name__)

_sleep = asyncio.sleep  # patched in tests

#: Longest pause taken for a budget window or a 429's Retry-After before giving up.
MAX_WAIT = 65.0
#: Bytes of an error body kept (enough for a message, never a whole page).
ERROR_BODY_BYTES = 4096

# C0/C1 controls (terminal escapes), DEL, and bidi overrides. Tab, newline and
# form feed survive in long text.
_TEXT_UNSAFE = re.compile(r"[\x00-\x08\x0b\x0d-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")
_TAG = re.compile(r"<[^<>]{0,200}>")


def clean_line(value: Any, limit: int = 300) -> str:
    """One printable line from an API value: no control characters, capped at *limit*."""
    if value is None:
        return ""
    text = safe_text(" ".join(str(value).split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean_text(value: Any, limit: int = 5_000_000) -> str:
    """Long API text with terminal escapes and bidi overrides removed, capped at *limit*."""
    if not value:
        return ""
    text = _TEXT_UNSAFE.sub("", str(value).replace("\r\n", "\n"))
    if len(text) > limit:
        text = text[:limit] + f"\n[truncated by openfoia at {limit:,} characters]"
    return text


def strip_tags(value: Any, limit: int = 500) -> str:
    """A one-line snippet with HTML tags (search highlights) removed."""
    return clean_line(_TAG.sub("", str(value or "")), limit)


def no_markup(text: str) -> str:
    """*text* with square brackets swapped for parentheses, so Rich never reads it as markup."""
    return text.replace("[", "(").replace("]", ")")


async def read_capped(
    egress: EgressPolicy,
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    max_bytes: int,
    label: str,
    timeout: float = 30.0,
) -> tuple[int, bytes, float | None]:
    """Status, body and Retry-After seconds for one GET; redirects are not followed.

    The body of a success over *max_bytes* raises AdapterRequestError; an
    error body is cut at ERROR_BODY_BYTES (only a message is wanted from it).

    The transport's own retries are turned off (``max_attempts=1``): each try
    is a request against the provider's limit, so the caller sends, counts and
    retries every one itself. The route (direct or Tor) is unchanged.
    """
    async with (
        egress_client(
            replace(egress, max_attempts=1), timeout=timeout, follow_redirects=False
        ) as client,
        client.stream("GET", url, params=params, headers=headers) as resp,
    ):
        cap = max_bytes if resp.status_code < 400 else ERROR_BODY_BYTES
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > cap:
                if resp.status_code >= 400:
                    del body[cap:]
                    break
                raise AdapterRequestError(
                    f"{label} response over {max_bytes // (1024 * 1024)} MB; refusing to read it."
                )
        return resp.status_code, bytes(body), _retry_after(resp)


def error_detail(body: bytes, limit: int = 200) -> str:
    """The message in an API error body (JSON ``message``/``error``/``detail``, else text)."""
    text = ""
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("message", "error", "detail", "errors"):
            value = data.get(key)
            if isinstance(value, str) and value:
                text = value
                break
            if isinstance(value, list) and value:
                first = value[0]
                text = first.get("error") if isinstance(first, dict) else str(first)
                text = str(text or "")
                break
    elif body:
        text = strip_tags(body.decode("utf-8", "replace"), limit)
    return no_markup(clean_line(text, limit))


def when(ts: float, now: float) -> str:
    """``2026-11-19 15:04 UTC (in 3 min)``."""
    at = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")
    minutes = max(1, round((ts - now) / 60))
    span = f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"
    return f"{at} (in {span})"


class RequestBudget:
    """Requests sent from this machine to one provider, in rolling windows.

    *limits* is a tuple of ``(window seconds, requests, label)``. When a window
    is full, :meth:`reserve` waits for the oldest request to age out if that is
    at most MAX_WAIT seconds away, and otherwise raises AdapterRequestError
    before anything is sent. A 429 that names a long wait is remembered with
    :meth:`block_until`, so later calls stop locally instead of hammering the
    API. Setting *ignore_env* skips the limits (the count is still kept).

    The count is advisory: it only sees this machine, and the file lives in the
    data directory, so ``purge`` removes it with everything else.
    """

    def __init__(
        self,
        *,
        provider: str,
        filename: str,
        limits: tuple[tuple[int, int, str], ...],
        ignore_env: str,
        usage_hint: str = "",
    ) -> None:
        self.provider = provider
        self.filename = filename
        self.limits = limits
        self.ignore_env = ignore_env
        self.usage_hint = usage_hint

    def path(self):
        return get_data_dir() / self.filename

    def _load(self, now: float) -> dict[str, Any]:
        try:
            data = json.loads(self.path().read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        longest = max(window for window, _, _ in self.limits)
        sent = [
            t
            for t in (data.get("sent") or [])
            if isinstance(t, int | float) and 0 <= now - t < longest
        ]
        blocked = data.get("blocked_until")
        return {
            "sent": sorted(sent),
            "blocked_until": blocked
            if isinstance(blocked, int | float) and blocked > now
            else None,
        }

    def _save(self, state: dict[str, Any]) -> None:
        path = self.path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state))
        except OSError as exc:  # advisory; never block on it
            logger.debug("Could not write %s budget file: %s", self.provider, exc)

    def used(self, now: float | None = None) -> dict[str, int]:
        """Requests counted in each window: ``{label: n}``."""
        now = time.time() if now is None else now
        sent = self._load(now)["sent"]
        return {label: sum(now - t < window for t in sent) for window, _, label in self.limits}

    async def reserve(self) -> None:
        """Count one request; wait out a short window, raise when a long one is full."""
        now = time.time()
        state = self._load(now)
        if os.environ.get(self.ignore_env):
            state["sent"].append(now)
            self._save(state)
            return
        if state["blocked_until"]:
            raise AdapterRequestError(
                f"{self.provider} rate limit: the server asked us to wait until "
                f"{when(state['blocked_until'], now)} (HTTP 429). Nothing was sent."
            )
        sent = state["sent"]
        for window, limit, label in sorted(self.limits, reverse=True):
            recent = [t for t in sent if now - t < window]
            if len(recent) < limit:
                continue
            frees = recent[len(recent) - limit] + window
            wait = max(0.0, frees - now)
            if wait > MAX_WAIT:
                hint = f" {self.usage_hint}" if self.usage_hint else ""
                raise AdapterRequestError(
                    f"{self.provider} request budget used up: {len(recent)} of {limit} "
                    f"requests in {label} (counted on this machine). Nothing was sent. The "
                    f"next request frees up at {when(frees, now)}.{hint} To skip this local "
                    f"limit, set {self.ignore_env}=1."
                )
            logger.warning(
                "%s allows %d requests in %s; waiting %.0fs.", self.provider, limit, label, wait
            )
            await _sleep(wait)
            now = time.time()
        sent.append(now)
        self._save(state)

    def block_until(self, ts: float) -> None:
        state = self._load(time.time())
        state["blocked_until"] = ts
        self._save(state)
