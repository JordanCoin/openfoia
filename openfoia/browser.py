"""Browser detection and launching with privacy options.

Supports:
- Safari (Private Window)
- Firefox (Private Window)
- Chrome/Chromium (Incognito)
- Brave (Private with Tor option)
- Tor Browser
- User's default browser

For transparency work, we recommend private/incognito mode to avoid
browser extensions potentially logging localhost traffic.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class BrowserType(str, Enum):
    SAFARI = "safari"
    FIREFOX = "firefox"
    CHROME = "chrome"
    CHROMIUM = "chromium"
    BRAVE = "brave"
    TOR = "tor"
    EDGE = "edge"
    DEFAULT = "default"


@dataclass
class Browser:
    """A detected browser with its capabilities."""

    browser_type: BrowserType
    name: str
    path: str | None
    supports_private: bool = True
    supports_tor: bool = False  # Brave has built-in Tor

    def __str__(self) -> str:
        return self.name


# macOS browser paths
MACOS_BROWSERS = {
    BrowserType.SAFARI: "/Applications/Safari.app",
    BrowserType.FIREFOX: "/Applications/Firefox.app",
    BrowserType.CHROME: "/Applications/Google Chrome.app",
    BrowserType.CHROMIUM: "/Applications/Chromium.app",
    BrowserType.BRAVE: "/Applications/Brave Browser.app",
    BrowserType.TOR: "/Applications/Tor Browser.app",
    BrowserType.EDGE: "/Applications/Microsoft Edge.app",
}

# Linux browser commands
LINUX_BROWSERS = {
    BrowserType.FIREFOX: "firefox",
    BrowserType.CHROME: "google-chrome",
    BrowserType.CHROMIUM: "chromium",
    BrowserType.BRAVE: "brave-browser",
    BrowserType.TOR: "torbrowser-launcher",
}


def detect_browsers() -> list[Browser]:
    """Detect installed browsers on the system."""
    browsers: list[Browser] = []
    system = platform.system()

    if system == "Darwin":  # macOS
        for browser_type, app_path in MACOS_BROWSERS.items():
            if Path(app_path).exists():
                browsers.append(Browser(
                    browser_type=browser_type,
                    name=_get_browser_name(browser_type),
                    path=app_path,
                    supports_private=True,
                    supports_tor=browser_type in (BrowserType.TOR, BrowserType.BRAVE),
                ))

    elif system == "Linux":
        for browser_type, cmd in LINUX_BROWSERS.items():
            if shutil.which(cmd):
                browsers.append(Browser(
                    browser_type=browser_type,
                    name=_get_browser_name(browser_type),
                    path=cmd,
                    supports_private=True,
                    supports_tor=browser_type in (BrowserType.TOR, BrowserType.BRAVE),
                ))

    elif system == "Windows":
        # Windows browser detection
        chrome_paths = [
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for p in chrome_paths:
            if p.exists():
                browsers.append(Browser(
                    browser_type=BrowserType.CHROME,
                    name="Google Chrome",
                    path=str(p),
                ))
                break

        # Add more Windows paths as needed

    # Always add "default" option
    browsers.append(Browser(
        browser_type=BrowserType.DEFAULT,
        name="System Default",
        path=None,
        supports_private=False,
    ))

    return browsers


def _get_browser_name(browser_type: BrowserType) -> str:
    """Get human-readable browser name."""
    return {
        BrowserType.SAFARI: "Safari",
        BrowserType.FIREFOX: "Firefox",
        BrowserType.CHROME: "Google Chrome",
        BrowserType.CHROMIUM: "Chromium",
        BrowserType.BRAVE: "Brave",
        BrowserType.TOR: "Tor Browser",
        BrowserType.EDGE: "Microsoft Edge",
        BrowserType.DEFAULT: "System Default",
    }.get(browser_type, browser_type.value)


def launch_browser(
    url: str,
    browser: Browser | BrowserType | str | None = None,
    private: bool = True,
    tor_mode: bool = False,  # For Brave's private window with Tor
) -> bool:
    """Launch a browser with the given URL.

    Args:
        url: The URL to open
        browser: Browser to use (auto-detects if None)
        private: Open in private/incognito mode
        tor_mode: Use Tor (Brave only, or use Tor Browser)

    Returns:
        True if browser launched successfully
    """
    system = platform.system()

    # Resolve browser
    if browser is None:
        browsers = detect_browsers()
        # Prefer Tor if tor_mode requested
        if tor_mode:
            tor_browsers = [b for b in browsers if b.supports_tor]
            browser = tor_browsers[0] if tor_browsers else browsers[0]
        else:
            # Prefer privacy-focused browsers
            preferred_order = [
                BrowserType.BRAVE,
                BrowserType.FIREFOX,
                BrowserType.SAFARI,
                BrowserType.CHROME,
            ]
            for bt in preferred_order:
                for b in browsers:
                    if b.browser_type == bt:
                        browser = b
                        break
                if browser:
                    break
            if not browser:
                browser = browsers[0] if browsers else None

    if isinstance(browser, str):
        browser = BrowserType(browser.lower())

    if isinstance(browser, BrowserType):
        browsers = detect_browsers()
        browser = next((b for b in browsers if b.browser_type == browser), None)

    if not browser:
        # Fallback: use Python's webbrowser module
        import webbrowser
        webbrowser.open(url)
        return True

    try:
        if system == "Darwin":
            return _launch_macos(url, browser, private, tor_mode)
        elif system == "Linux":
            return _launch_linux(url, browser, private, tor_mode)
        elif system == "Windows":
            return _launch_windows(url, browser, private, tor_mode)
        else:
            import webbrowser
            webbrowser.open(url)
            return True
    except Exception as e:
        print(f"Failed to launch browser: {e}")
        return False


def _launch_macos(url: str, browser: Browser, private: bool, tor_mode: bool) -> bool:
    """Launch browser on macOS."""
    browser_type = browser.browser_type

    if browser_type == BrowserType.DEFAULT:
        subprocess.run(["open", url])
        return True

    if browser_type == BrowserType.SAFARI:
        if private:
            # Safari private window via AppleScript
            script = f'''
            tell application "Safari"
                activate
                tell application "System Events"
                    keystroke "n" using {{command down, shift down}}
                end tell
                delay 0.5
                set URL of document 1 to "{url}"
            end tell
            '''
            subprocess.run(["osascript", "-e", script])
        else:
            subprocess.run(["open", "-a", "Safari", url])
        return True

    if browser_type == BrowserType.FIREFOX:
        args = ["open", "-a", "Firefox", "--args"]
        if private:
            args.extend(["--private-window", url])
        else:
            args.append(url)
        subprocess.run(args)
        return True

    if browser_type == BrowserType.CHROME:
        args = ["open", "-a", "Google Chrome", "--args"]
        if private:
            args.extend(["--incognito", url])
        else:
            args.append(url)
        subprocess.run(args)
        return True

    if browser_type == BrowserType.BRAVE:
        args = ["open", "-a", "Brave Browser", "--args"]
        if tor_mode:
            args.extend(["--tor", url])
        elif private:
            args.extend(["--incognito", url])
        else:
            args.append(url)
        subprocess.run(args)
        return True

    if browser_type == BrowserType.TOR:
        subprocess.run(["open", "-a", "Tor Browser", url])
        return True

    # Generic fallback
    if browser.path:
        subprocess.run(["open", "-a", browser.path, url])
        return True

    return False


def _launch_linux(url: str, browser: Browser, private: bool, tor_mode: bool) -> bool:
    """Launch browser on Linux."""
    browser_type = browser.browser_type
    cmd = browser.path

    if not cmd:
        import webbrowser
        webbrowser.open(url)
        return True

    args = [cmd]

    if browser_type == BrowserType.FIREFOX:
        if private:
            args.extend(["--private-window", url])
        else:
            args.append(url)

    elif browser_type in (BrowserType.CHROME, BrowserType.CHROMIUM):
        if private:
            args.extend(["--incognito", url])
        else:
            args.append(url)

    elif browser_type == BrowserType.BRAVE:
        if tor_mode:
            args.extend(["--tor", url])
        elif private:
            args.extend(["--incognito", url])
        else:
            args.append(url)

    elif browser_type == BrowserType.TOR:
        args.append(url)

    else:
        args.append(url)

    subprocess.Popen(args, start_new_session=True)
    return True


def _launch_windows(url: str, browser: Browser, private: bool, tor_mode: bool) -> bool:
    """Launch browser on Windows."""
    browser_type = browser.browser_type
    path = browser.path

    if browser_type == BrowserType.DEFAULT or not path:
        os.startfile(url)
        return True

    args = [path]

    if browser_type == BrowserType.CHROME:
        if private:
            args.extend(["--incognito", url])
        else:
            args.append(url)

    elif browser_type == BrowserType.FIREFOX:
        if private:
            args.extend(["-private-window", url])
        else:
            args.append(url)

    else:
        args.append(url)

    subprocess.Popen(args)
    return True


def print_browser_menu(browsers: list[Browser]) -> None:
    """Print a menu of available browsers."""
    print("\n🌐 Available browsers:\n")
    for i, browser in enumerate(browsers, 1):
        extras = []
        if browser.supports_private:
            extras.append("private mode")
        if browser.supports_tor:
            extras.append("Tor support")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        print(f"  [{i}] {browser.name}{extra_str}")
    print()


# CLI helper for interactive browser selection
def select_browser_interactive(
    url: str,
    default_private: bool = True,
) -> None:
    """Interactive browser selection for CLI."""
    browsers = detect_browsers()

    if not browsers:
        print("No browsers detected. Opening with system default...")
        import webbrowser
        webbrowser.open(url)
        return

    print_browser_menu(browsers)

    try:
        choice = input("Select browser [1]: ").strip() or "1"
        idx = int(choice) - 1
        if idx < 0 or idx >= len(browsers):
            idx = 0
        browser = browsers[idx]
    except (ValueError, IndexError):
        browser = browsers[0]

    private = default_private
    if browser.supports_private:
        private_choice = input("Open in private/incognito mode? [Y/n]: ").strip().lower()
        private = private_choice != "n"

    tor_mode = False
    if browser.supports_tor:
        tor_choice = input("Use Tor for additional privacy? [y/N]: ").strip().lower()
        tor_mode = tor_choice == "y"

    print(f"\n🚀 Launching {browser.name}{'(private)' if private else ''}{'(Tor)' if tor_mode else ''}...")
    launch_browser(url, browser, private=private, tor_mode=tor_mode)
