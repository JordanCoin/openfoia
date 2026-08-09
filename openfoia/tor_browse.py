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

from pathlib import Path
from typing import Any

from rich import print as rprint


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOR_SOCKS_PROXY = "socks5://127.0.0.1:9050"

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
        raise SystemExit(1)

    if use_tor:
        rprint(_TOR_WARNING)

    # Note: "--disable-webrtc" is not a real Chromium switch; the WebRTC
    # mitigation that actually takes effect is the RTCPeerConnection removal
    # in the init script below. Keep the flags that do exist.
    launch_args: dict[str, Any] = {
        "headless": headless,
        "args": [
            "--disable-webgl",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-features=WebRtcHideLocalIpsWithMdns",
        ],
    }

    if use_tor:
        launch_args["proxy"] = {"server": TOR_SOCKS_PROXY}

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
            from datetime import datetime

            safe_name = re.sub(r"[^\w\-.]", "_", url.split("//", 1)[-1][:60])
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{safe_name}.txt"
            out_path = save_dir / filename

            out_path.write_text(
                f"URL: {result['url']}\n"
                f"Title: {result['title']}\n"
                f"Captured: {datetime.utcnow().isoformat()}Z\n"
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
            try:
                await page.wait_for_event("close", timeout=0)
            except KeyboardInterrupt:
                pass

        await browser.close()

    return result
