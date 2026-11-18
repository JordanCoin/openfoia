"""Security tests for portable mode, the web UI login, and `browse --tor`.

Companion to tests/test_audit_repros.py findings A7 (portable mode wrote to the
host's home directory), A10 (the web UI token sat in the browser's argv) and
A16 (`browse --tor` could never connect).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A16: `browse --tor` reaches the proxy, with hostnames, and no local DNS
# ---------------------------------------------------------------------------

_TEST_HOST = "openfoia-tor-check.example"
_PAGE = b"<html><head><title>via-socks</title></head><body>ok</body></html>"


class FakeSocks5:
    """A SOCKS5 listener that records each CONNECT and answers with one page."""

    def __init__(self):
        self.connects: list[tuple[int, str, int]] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self._sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _read(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("short read")
            buf += chunk
        return buf

    def _handle(self, conn):
        try:
            _ver, nmethods = self._read(conn, 2)
            self._read(conn, nmethods)
            conn.sendall(b"\x05\x00")  # no authentication
            _ver, _cmd, _rsv, atyp = self._read(conn, 4)
            if atyp == 0x03:
                host = self._read(conn, self._read(conn, 1)[0]).decode()
            elif atyp == 0x01:
                host = socket.inet_ntop(socket.AF_INET, self._read(conn, 4))
            else:
                host = socket.inet_ntop(socket.AF_INET6, self._read(conn, 16))
            (port,) = struct.unpack(">H", self._read(conn, 2))
            self.connects.append((atyp, host, port))
            conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                + f"Content-Length: {len(_PAGE)}\r\n\r\n".encode()
                + _PAGE
            )
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            conn.close()


def _chromium_executable() -> str | None:
    """A Chromium Playwright can drive, or None (the tests then skip)."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return None
    for candidate in (os.environ.get("OPENFOIA_TEST_CHROMIUM"), "/opt/pw-browsers/chromium"):
        if candidate and Path(candidate).is_file():
            return candidate
    # Never `playwright install` from a test: use what is already there.
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            path = pw.chromium.executable_path
    except Exception:
        return None
    return path if path and Path(path).is_file() else None


async def _chromium_scenario(mode: str, exe: str, home: str) -> dict:
    """Open http://_TEST_HOST/ with the launch options `browse` would use.

    mode "tor": through a fake SOCKS5 proxy. "dns-guard": no proxy, but the
    Tor-mode DNS switch. "direct": plain Chromium (the control).
    """
    from playwright.async_api import async_playwright

    from openfoia.tor_browse import _launch_options

    proxy = FakeSocks5() if mode == "tor" else None
    options = _launch_options(
        use_tor=mode in ("tor", "dns-guard"),
        headless=True,
        tor_host="127.0.0.1",
        tor_port=proxy.port if proxy else 9,
        home=Path(home),
    )
    if mode == "dns-guard":
        del options["proxy"]
    if mode == "direct":
        options["args"] = []
    options["executable_path"] = exe
    out: dict = {"mode": mode}
    try:
        async with async_playwright() as pw:
            b = await pw.chromium.launch(**options)
            page = await b.new_page()
            try:
                await page.goto(f"http://{_TEST_HOST}/", timeout=20000)
                out["title"] = await page.title()
            except Exception as e:
                out["error"] = str(e).splitlines()[0]
            await b.close()
    finally:
        if proxy:
            out["connects"] = list(proxy.connects)
            proxy.close()
    return out


def _scenario_main(mode: str, exe: str, home: str) -> None:
    """Entry point for the strace'd subprocess."""
    import json

    print("RESULT " + json.dumps(asyncio.run(_chromium_scenario(mode, exe, home))), flush=True)


def test_tor_launch_options_use_a_scheme_chromium_supports():
    from openfoia.tor_browse import _launch_options, _no_local_dns_arg

    opts = _launch_options(use_tor=True, headless=True, tor_host="127.0.0.1", tor_port=9050)

    assert opts["proxy"] == {"server": "socks5://127.0.0.1:9050"}
    assert _no_local_dns_arg("127.0.0.1") in opts["args"]
    assert _no_local_dns_arg("127.0.0.1") == (
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"
    )
    ipv6 = _launch_options(use_tor=True, headless=True, tor_host="::1", tor_port=9150)
    assert ipv6["proxy"] == {"server": "socks5://[::1]:9150"}
    direct = _launch_options(use_tor=False, headless=True)
    assert "proxy" not in direct
    assert not any(a.startswith("--host-resolver-rules") for a in direct["args"])


def test_browser_process_gets_a_home_inside_the_scratch_dir(tmp_path):
    from openfoia.tor_browse import _launch_options

    env = _launch_options(use_tor=True, headless=True, home=tmp_path)["env"]

    assert env["HOME"] == str(tmp_path)
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        assert env[var].startswith(str(tmp_path)), var


def test_tor_browse_sends_hostnames_to_the_socks_proxy(tmp_path, monkeypatch):
    """Real Chromium, fake SOCKS5: the page loads and the CONNECT carries the name."""
    exe = _chromium_executable()
    if exe is None:
        pytest.skip("no Playwright Chromium available")
    fake_home = tmp_path / "host-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    scratch = tmp_path / "browser-home"
    scratch.mkdir()

    out = asyncio.run(_chromium_scenario("tor", exe, str(scratch)))

    assert out.get("title") == "via-socks", out
    to_target = [c for c in out["connects"] if c[1] == _TEST_HOST or c[2] == 80]
    assert (0x03, _TEST_HOST, 80) in to_target, out
    assert all(atyp == 0x03 for atyp, _, _ in to_target), f"IP CONNECT = local DNS: {out}"
    assert sorted(p.name for p in fake_home.iterdir()) == [], "the browser wrote to $HOME"


def _strace_scenario(tmp_path, mode, exe):
    import json

    log = tmp_path / f"strace-{mode}.log"
    home = tmp_path / f"home-{mode}"
    home.mkdir()
    code = (
        "from tests.test_security_webui_config import _scenario_main; "
        f"_scenario_main({mode!r}, {exe!r}, {str(home)!r})"
    )
    trace_calls = "trace=connect,sendto,sendmsg,sendmmsg"
    proc = subprocess.run(
        ["strace", "-f", "-qq", "-e", trace_calls, "-o", str(log), sys.executable, "-c", code],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=180,
    )
    result = next(
        (json.loads(ln[7:]) for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None
    )
    if result is None:
        pytest.skip(f"could not run Chromium under strace: {proc.stderr[-400:]}")
    # Every syscall on a socket connected to port 53: the connect itself and the
    # queries sent on it (strace shows the first bytes, which hold the name).
    import re

    dns_fds: set[tuple[str, str]] = set()
    dns: list[str] = []
    for line in log.read_text(errors="replace").splitlines():
        m = re.match(r"(\d+)\s+(connect|sendto|sendmsg|sendmmsg)\((\d+),", line)
        if not m:
            continue
        pid, call, fd = m.groups()
        if call == "connect":
            if "htons(53)" in line:
                dns_fds.add((pid, fd))
                dns.append(line)
            else:
                dns_fds.discard((pid, fd))
        elif (pid, fd) in dns_fds:
            dns.append(line)
    return result, dns


def _mentions(lines, host):
    """Whether a DNS query for *host* appears (labels are length-prefixed)."""
    first_label = host.split(".")[0]
    return any(first_label in ln for ln in lines)


def test_tor_browse_makes_no_local_dns_lookup(tmp_path):
    """Watch the browser's syscalls: in Tor mode nothing goes to port 53."""
    exe = _chromium_executable()
    if exe is None:
        pytest.skip("no Playwright Chromium available")
    if shutil.which("strace") is None:
        pytest.skip("strace not installed")

    control, control_dns = _strace_scenario(tmp_path, "direct", exe)
    if not _mentions(control_dns, _TEST_HOST):
        pytest.skip(f"cannot observe DNS queries on this machine (control: {control})")

    # The Tor-mode DNS switch alone (no proxy) must stop the lookup of the
    # page's name. Full Chromium (not the headless shell) then runs its own
    # "DNS probe" for google.com to explain the error page; that probe never
    # carries the visited name and does not fire in Tor mode, where a name Tor
    # cannot resolve is a SOCKS error, not a DNS error.
    guard, guard_dns = _strace_scenario(tmp_path, "dns-guard", exe)
    assert "ERR_NAME_NOT_RESOLVED" in guard.get("error", ""), guard
    assert not _mentions(guard_dns, _TEST_HOST), f"local DNS for the page: {guard_dns[:4]}"

    tor, tor_dns = _strace_scenario(tmp_path, "tor", exe)
    assert tor.get("title") == "via-socks", tor
    assert [0x03, _TEST_HOST, 80] in tor["connects"], tor
    assert tor_dns == [], f"local DNS lookup in Tor mode: {tor_dns[:3]}"
