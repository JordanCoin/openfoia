"""Agentic Tor browsing with Playwright.

Provides a CLI-accessible browser automation layer with optional Tor routing,
fingerprint hardening, and page content extraction.  This is a foundation for
future agentic browsing workflows, not a full automation framework.

Requirements:
    pip install 'openfoia[browser]'   # installs playwright
    playwright install chromium        # one-time browser download

Tor daemon must be running locally on port 9050 when --tor is used.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from rich import print as rprint

from .models import utcnow as _utcnow

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A common, non-unique user-agent string to blend in
_COMMON_UA = "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0"

_TOR_WARNING = """
[bold yellow]Tor Browsing Session[/bold yellow]
[yellow]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/yellow]
[yellow]Traffic will be routed through the Tor network (SOCKS5 localhost:9050).
The Tor daemon must be running. On macOS: brew install tor && tor
On Debian/Tails: sudo apt install tor && sudo systemctl start tor

Best-effort fingerprint hardening is applied:
  - WebGL vendor/renderer spoofed
  - RTCPeerConnection removed from the page (reduces, does not eliminate,
    the risk of a WebRTC IP leak)
  - Timezone forced to UTC
  - Common user-agent applied

These are page-level mitigations, NOT the Tor Browser's defenses. A
determined site can still fingerprint this browser, and script execution
inside the page may find ways around the patches above. For real anonymity
use the Tor Browser itself, or Tails. Avoid logging into personal accounts.
Do not download files you do not trust.
[/yellow]
[yellow]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/yellow]
"""


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _tor_proxy_server(tor_host: str, tor_port: int) -> str:
    """Build the Playwright proxy server string for *tor_host*:*tor_port*.

    socks5://, not socks5h://: Chromium does not know the socks5h scheme and
    fails every request with ERR_NO_SUPPORTED_PROXIES. Through a socks5://
    proxy Chromium already sends the hostname to the proxy (a domain-name
    CONNECT, ATYP 0x03) instead of resolving it locally, so Tor does the DNS.
    """
    return f"socks5://{_bracket(tor_host)}:{tor_port}"


def _no_local_dns_arg(tor_host: str) -> str:
    """Chromium switch that makes every local DNS lookup fail.

    Defence in depth behind the socks5:// proxy: if any code path in the
    browser tried to resolve a name itself, it gets NOTFOUND rather than
    asking the local resolver (which would tell the network what is being
    visited). The proxy host is excluded so a name like "localhost" works.
    Syntax per Chromium's SOCKS proxy documentation; no shell quoting, as
    Playwright passes it as a single argv entry.
    """
    return f"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE {tor_host}"


def _browser_env(home: Path) -> dict[str, str]:
    """Environment for the browser process with its home inside *home*.

    Chromium writes crash-report databases and caches under $HOME
    (~/.config/<product>/Crash Reports, ~/.cache, ~/.pki) even with a
    throwaway profile. Pointing HOME and the XDG dirs at a scratch dir inside
    the data dir keeps that off the host in portable mode, and in the data
    dir (which purge covers) otherwise.
    """
    import os

    env = dict(os.environ)
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
    )
    return env


def _launch_options(
    *,
    use_tor: bool,
    headless: bool,
    tor_host: str = "127.0.0.1",
    tor_port: int = 9050,
    home: Path | None = None,
) -> dict[str, Any]:
    """Keyword arguments for Playwright's ``chromium.launch()``."""
    # Note: "--disable-webrtc" is not a real Chromium switch; the WebRTC
    # mitigation that actually takes effect is the RTCPeerConnection removal
    # in the init script below. Keep the flags that do exist.
    options: dict[str, Any] = {
        "headless": headless,
        "args": [
            "--disable-webgl",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-features=WebRtcHideLocalIpsWithMdns",
        ],
    }
    if use_tor:
        options["proxy"] = {"server": _tor_proxy_server(tor_host, tor_port)}
        options["args"].append(_no_local_dns_arg(tor_host))
    if home is not None:
        options["env"] = _browser_env(home)
    return options


# ---------------------------------------------------------------------------
# Core browsing function
# ---------------------------------------------------------------------------


async def browse(
    url: str,
    *,
    use_tor: bool = False,
    headless: bool = False,
    save: bool = False,
    save_dir: Path | None = None,
) -> dict[str, Any]:
    """Launch a hardened browser session, navigate to *url*, and optionally
    save the page content.

    Returns a dict with keys:
        title: page title
        url: final URL after redirects
        content: extracted text (if *save* is True)
        saved_to: file path (if *save* is True)
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        rprint(
            "[red]Browser automation not installed.[/red]\n"
            "[yellow]Run: openfoia install-extras browser[/yellow]\n"
            "[dim]Then: playwright install chromium[/dim]"
        )
        raise SystemExit(1) from None

    if use_tor:
        rprint(_TOR_WARNING)

    import tempfile

    from .config import load_config
    from .db import _ensure_private_dir, get_data_dir

    net_cfg = load_config().network

    # The browser's own $HOME: a scratch dir in the data dir, removed on exit.
    browser_home = tempfile.TemporaryDirectory(
        dir=_ensure_private_dir(get_data_dir() / "browser_tmp"),
        prefix="home-",
        ignore_cleanup_errors=True,
    )
    launch_args = _launch_options(
        use_tor=use_tor,
        headless=headless,
        tor_host=net_cfg.tor_host,
        tor_port=net_cfg.tor_port,
        home=Path(browser_home.name),
    )
    try:
        return await _run_session(
            async_playwright, launch_args, url, use_tor, headless, save, save_dir
        )
    finally:
        browser_home.cleanup()


async def _run_session(
    async_playwright: Any,
    launch_args: dict[str, Any],
    url: str,
    use_tor: bool,
    headless: bool,
    save: bool,
    save_dir: Path | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_args)

        context = await browser.new_context(
            user_agent=_COMMON_UA,
            timezone_id="UTC",
            locale="en-US",
            # Disable geolocation
            geolocation=None,
            permissions=[],
            # Viewport that blends in
            viewport={"width": 1280, "height": 800},
        )

        # Inject JS to harden fingerprint further
        await context.add_init_script("""
            // Disable WebGL fingerprinting
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(param) {
                if (param === 37445) return 'Intel Inc.';
                if (param === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.call(this, param);
            };

            // Disable WebRTC IP leak
            if (typeof RTCPeerConnection !== 'undefined') {
                RTCPeerConnection = undefined;
            }
            if (typeof webkitRTCPeerConnection !== 'undefined') {
                webkitRTCPeerConnection = undefined;
            }

            // Spoof navigator properties
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)

        page = await context.new_page()

        rprint(f"[cyan]Navigating to:[/cyan] {url}")
        if use_tor:
            rprint("[dim]Routing through Tor... this may be slow.[/dim]")

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            status = response.status if response else "unknown"
            rprint(f"[green]Loaded[/green] (HTTP {status})")
        except Exception as e:
            rprint(f"[red]Navigation failed:[/red] {e}")
            await browser.close()
            raise

        result["title"] = await page.title()
        result["url"] = page.url

        if save:
            # Extract readable text content
            content = await page.evaluate("""
                () => {
                    // Remove script and style elements
                    const clone = document.cloneNode(true);
                    clone.querySelectorAll('script, style, noscript').forEach(el => el.remove());
                    return clone.body ? clone.body.innerText : document.documentElement.innerText;
                }
            """)
            result["content"] = content

            # Save to file
            if save_dir is None:
                from .db import get_data_dir

                save_dir = get_data_dir() / "browse"

            save_dir.mkdir(parents=True, exist_ok=True)

            # Sanitize filename from URL
            import re

            safe_name = re.sub(r"[^\w\-.]", "_", url.split("//", 1)[-1][:60])
            timestamp = _utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{safe_name}.txt"
            out_path = save_dir / filename

            out_path.write_text(
                f"URL: {result['url']}\n"
                f"Title: {result['title']}\n"
                f"Captured: {_utcnow().isoformat()}Z\n"
                f"Tor: {use_tor}\n"
                f"{'=' * 60}\n\n"
                f"{content}"
            )
            result["saved_to"] = str(out_path)
            rprint(f"[green]Saved to:[/green] {out_path}")

        if not headless:
            rprint(
                "\n[dim]Browser is open. Close the browser window or press Ctrl+C to exit.[/dim]"
            )
            with contextlib.suppress(KeyboardInterrupt):
                await page.wait_for_event("close", timeout=0)

        await browser.close()

    return result
