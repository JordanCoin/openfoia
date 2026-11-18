"""Security tests for portable mode, the web UI login, `browse --tor` and CI.

Companion to tests/test_audit_repros.py findings A7 (portable mode wrote to the
host's home directory), A10 (the web UI token sat in the browser's argv), A16
(`browse --tor` could never connect) and A17 (the release job ran unpinned
actions with a write token).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import site
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A7: portable mode leaves nothing in the host's home directory
# ---------------------------------------------------------------------------

#: Variables that would let a library write somewhere other than $HOME. They
#: are removed so that anything defaulting to the home directory lands in the
#: empty fake one, where the test sees it.
_HOME_REDIRECTS = (
    "OPENFOIA_DATA_DIR",
    "OPENFOIA_DB_PASSWORD",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "HF_HOME",
    "TORCH_HOME",
    "MPLCONFIGDIR",
)


def _run_cli(args, *, cwd, home):
    env = {k: v for k, v in os.environ.items() if k not in _HOME_REDIRECTS}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        OPENFOIA_AI_PROVIDER="none",
        PYTHONPATH=str(REPO),
        PYTHONDONTWRITEBYTECODE="1",
        # Packages installed with `pip install --user` live under the real
        # home; keep them importable (read-only) without un-faking $HOME.
        PYTHONUSERBASE=site.getuserbase(),
    )
    return subprocess.run(
        [sys.executable, "-c", "from openfoia.cli import app; app()", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_portable_session_leaves_host_home_empty(tmp_path):
    """init, ingest, graph and purge from a portable stick write nothing to $HOME."""
    home = tmp_path / "host-home"
    home.mkdir()
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / ".openfoia-portable").touch()
    doc = usb / "memo.txt"
    doc.write_text("Jane Doe of Acme Corp met the minister in Ankara on 3 March 2026.\n")

    for args in (
        ["init"],
        ["docs", "ingest", str(doc)],
        ["analyze", "graph", "--all", "--output", str(usb / "graph.json")],
        ["purge", "--yes"],
    ):
        result = _run_cli(args, cwd=usb, home=home)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        left = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
        assert left == [], f"`openfoia {' '.join(args)}` wrote to the host home: {left}"

    assert not (usb / "openfoia-data" / "data.db").exists(), "purge left the database"


def test_portable_mode_points_library_caches_at_the_stick(tmp_path, monkeypatch):
    """Optional deps (GLiNER via huggingface_hub, torch) cache under $HOME by default."""
    from openfoia.config import _CACHE_ENV, confine_caches_to_data_dir

    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / ".openfoia-portable").touch()
    monkeypatch.chdir(usb)
    monkeypatch.delenv("OPENFOIA_DATA_DIR", raising=False)
    for var in _CACHE_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TORCH_HOME", "/user/chose/this")

    applied = confine_caches_to_data_dir()

    assert os.environ["HF_HOME"] == str(usb / "openfoia-data" / "cache" / "huggingface")
    assert os.environ["XDG_CACHE_HOME"] == str(usb / "openfoia-data" / "cache")
    assert os.environ["TORCH_HOME"] == "/user/chose/this", "an explicit setting was overridden"
    assert "TORCH_HOME" not in applied
    assert not (usb / "openfoia-data").exists(), "resolving the caches must create nothing"


def test_caches_stay_put_outside_portable_mode(tmp_path, monkeypatch):
    from openfoia.config import _CACHE_ENV, confine_caches_to_data_dir

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENFOIA_DATA_DIR", raising=False)
    for var in _CACHE_ENV:
        monkeypatch.delenv(var, raising=False)

    assert confine_caches_to_data_dir() == {}
    assert "HF_HOME" not in os.environ


def test_config_data_dir_follows_portable_mode(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / ".openfoia-portable").touch()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENFOIA_DATA_DIR", raising=False)
    monkeypatch.chdir(usb)

    from openfoia.config import OpenFOIAConfig

    assert OpenFOIAConfig().data_dir == usb / "openfoia-data"
    assert list(home.iterdir()) == []


# ---------------------------------------------------------------------------
# A10: the web UI token never travels in a URL or on a command line
# ---------------------------------------------------------------------------

TOKEN = "long-lived-session-token"


@pytest.fixture
def webui_dir(tmp_path, monkeypatch):
    """A data dir with an initialised database, as `openfoia serve` would use."""
    d = tmp_path / "data"
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(d))
    monkeypatch.delenv("OPENFOIA_DB_PASSWORD", raising=False)
    from openfoia.db import init_db

    init_db()
    return d


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _app(webui_dir, clock=None):
    from openfoia.server import LoginNonces, create_app

    nonces = LoginNonces(clock=clock) if clock else LoginNonces()
    return create_app(token=TOKEN, data_dir=webui_dir, nonces=nonces)


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="http://127.0.0.1:8765", follow_redirects=False)


def _login(client, app):
    nonce = app.state.login_nonces.issue()
    return nonce, client.get("/login", params={"nonce": nonce})


def test_login_swaps_nonce_for_strict_httponly_cookie(webui_dir):
    app = _app(webui_dir)
    client = _client(app)

    _, resp = _login(client, app)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/", "the redirect must land on a clean URL"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"openfoia_session={TOKEN};")
    attrs = {a.strip().lower() for a in cookie.split(";")[1:]}
    assert "httponly" in attrs
    assert "samesite=strict" in attrs
    assert "path=/" in attrs
    assert resp.headers["referrer-policy"] == "no-referrer"

    page = client.get("/")
    assert page.status_code == 200 and "<title>OpenFOIA</title>" in page.text
    assert client.get("/api/stats").status_code != 401


def test_used_login_nonce_is_worthless(webui_dir, capsys):
    app = _app(webui_dir)
    nonce, first = _login(_client(app), app)
    assert first.status_code == 303

    attacker = _client(app)  # a second client, with no cookie of its own
    replay = attacker.get("/login", params={"nonce": nonce})

    assert replay.status_code == 403
    assert "set-cookie" not in replay.headers
    assert TOKEN not in replay.text
    assert attacker.get("/api/stats").status_code == 401
    assert attacker.get("/").status_code == 401
    assert "already used" in capsys.readouterr().err


def test_expired_login_nonce_is_refused(webui_dir):
    clock = _Clock()
    app = _app(webui_dir, clock)
    client = _client(app)
    nonce = app.state.login_nonces.issue()

    clock.now += 61
    resp = client.get("/login", params={"nonce": nonce})

    assert resp.status_code == 401
    assert "set-cookie" not in resp.headers


def test_wrong_or_missing_nonce_is_refused(webui_dir):
    app = _app(webui_dir)
    client = _client(app)
    real = app.state.login_nonces.issue()
    near_miss = real[:-1] + ("A" if real[-1] != "A" else "B")

    for params in ({"nonce": near_miss}, {"nonce": ""}, {}):
        resp = client.get("/login", params=params)
        assert resp.status_code == 401, params
        assert "set-cookie" not in resp.headers
    # The near miss did not burn the real nonce.
    assert client.get("/login", params={"nonce": real}).status_code == 303


def test_nonces_are_single_use_and_independent():
    from openfoia.server import LoginNonces, NonceResult

    clock = _Clock()
    nonces = LoginNonces(ttl=60, clock=clock)
    a, b = nonces.issue(), nonces.issue()
    assert a != b and len(a) >= 32

    assert nonces.consume(a) is NonceResult.OK
    assert nonces.consume(a) is NonceResult.USED
    assert nonces.consume(b) is NonceResult.OK
    assert nonces.consume("\udcff not ascii \u00e9") is NonceResult.INVALID
    assert nonces.consume(None) is NonceResult.INVALID

    c = nonces.issue()
    clock.now += 60
    assert nonces.consume(c) is NonceResult.INVALID


def test_query_string_token_is_not_accepted(webui_dir):
    client = _client(_app(webui_dir))

    assert client.get("/api/stats", params={"token": TOKEN}).status_code == 401
    assert client.get("/", params={"token": TOKEN}).status_code == 401


def test_bearer_header_is_accepted_for_api_clients(webui_dir):
    client = _client(_app(webui_dir))

    ok = client.get("/api/stats", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    assert client.get("/api/stats", headers={"Authorization": "Bearer nope"}).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-site"},  # a page on another 127.0.0.1 port
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://127.0.0.1:9999"},
        {"Origin": "null"},
    ],
)
def test_session_cookie_is_ignored_on_requests_from_another_origin(webui_dir, headers):
    """The cookie is same-site for every local port; the origin must match exactly."""
    app = _app(webui_dir)
    client = _client(app)
    _login(client, app)

    assert client.get("/api/stats").status_code == 200
    assert client.get("/api/stats", headers=headers).status_code == 401
    resp = client.post(
        "/api/requests",
        json={"agency_id": "x", "subject": "s", "body": "b"},
        headers=headers,
    )
    assert resp.status_code == 401


def test_same_origin_fetch_with_cookie_is_accepted(webui_dir):
    app = _app(webui_dir)
    client = _client(app)
    _login(client, app)

    resp = client.get(
        "/api/stats",
        headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:8765"},
    )
    assert resp.status_code == 200


def test_no_cors_allowance_for_other_local_origins(webui_dir):
    client = _client(_app(webui_dir))

    resp = client.options(
        "/api/stats",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in resp.headers
    assert "access-control-allow-credentials" not in resp.headers


def test_web_ui_script_never_reads_a_token_from_the_url():
    from openfoia.server import get_index_html

    html = get_index_html()
    assert "token=" not in html
    assert "get('token')" not in html


@pytest.mark.parametrize(
    "path,host",
    [
        ("/api/health", "127.0.0.1:8765"),
        ("/static/app.css", "127.0.0.1:8765"),
        ("/", "127.0.0.1:8765"),
        ("/api/stats", "127.0.0.1:8765"),
        ("/login", "127.0.0.1:8765"),
        ("/no-such-page", "127.0.0.1:8765"),
        ("/api/health", "rebind.attacker.example"),
    ],
)
def test_every_response_refuses_cross_origin_embedding(webui_dir, path, host):
    """A hostile page must not be able to detect OpenFOIA by loading its resources."""
    client = _client(_app(webui_dir))

    resp = client.get(path, headers={"Host": host})

    assert resp.headers.get("cross-origin-resource-policy") == "same-origin", (path, host)
    assert resp.headers.get("x-content-type-options") == "nosniff", (path, host)


def test_static_stylesheet_is_actually_served(webui_dir):
    resp = _client(_app(webui_dir)).get("/static/app.css")
    assert resp.status_code == 200


def test_serve_prints_and_opens_a_login_link_not_the_token(webui_dir, monkeypatch, capsys):
    import uvicorn

    from openfoia import server

    opened = []

    class _FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False

        def run(self):
            self.started = True
            for _ in range(200):  # let the opener thread see `started`
                if opened:
                    break
                time.sleep(0.01)

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    server.run_server(port=8765, token=TOKEN, data_dir=webui_dir, open_browser=opened.append)

    out = capsys.readouterr().out
    assert TOKEN not in out
    assert opened and TOKEN not in opened[0]
    assert opened[0].startswith("http://127.0.0.1:8765/login?nonce=")
    assert opened[0] in out, "the printed link must be the same single-use link"
    assert str(webui_dir) in out, "the banner names the real data dir"


def test_browser_launchers_never_put_a_token_on_argv(monkeypatch):
    from openfoia import browser

    seen = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **kw: seen.append(args))
    monkeypatch.setattr(browser.subprocess, "run", lambda args, **kw: seen.append(args))
    url = "http://127.0.0.1:8765/?token=SESSION-SECRET&view=graph"
    chromium = browser.Browser(browser.BrowserType.CHROMIUM, "Chromium", "/usr/bin/chromium")
    chrome = browser.Browser(browser.BrowserType.CHROME, "Chrome", r"C:\chrome.exe")
    safari = browser.Browser(browser.BrowserType.SAFARI, "Safari", None)

    browser._launch_linux(url, chromium, True, False)
    browser._launch_windows(url, chrome, True, False)
    browser._launch_macos(url, safari, True, False)
    browser._launch_macos(url, chrome, False, False)

    flat = [str(a) for args in seen for a in args]
    assert len(seen) == 4
    assert not any("SESSION-SECRET" in a for a in flat), seen
    assert any("view=graph" in a for a in flat), "unrelated parameters are kept"


def test_browser_launch_keeps_the_login_nonce(monkeypatch):
    from openfoia import browser

    seen = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **kw: seen.append(args))
    b = browser.Browser(browser.BrowserType.FIREFOX, "Firefox", "/usr/bin/firefox")
    url = "http://127.0.0.1:8765/login?nonce=abc123"

    browser._launch_linux(url, b, True, False)

    assert seen == [["/usr/bin/firefox", "--private-window", url]]


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
