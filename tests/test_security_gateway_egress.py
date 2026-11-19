"""Delivery gateways follow the egress policy: Tor mode goes through Tor or not at all.

`request send` used to warn in Tor mode that email, fax and mail all went
direct, then send anyway (silently, in a non-interactive run). Now SMTP goes
through the Tor SOCKS proxy (a stdlib SOCKS5 client), and Twilio and Lob are
called over their REST APIs through egress_client. A local fake SOCKS5
server records what the client asks for: the destination must arrive as a
hostname (resolved at the exit), and nothing may be resolved locally.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
import threading
from urllib.parse import parse_qs

import httpx
import pytest
from typer.testing import CliRunner

from openfoia.gateways import email as email_mod
from openfoia.gateways import fax as fax_mod
from openfoia.gateways import http as http_mod
from openfoia.gateways import mail as mail_mod
from openfoia.gateways.base import DeliveryPayload, DeliveryResult, DeliveryStatus
from openfoia.net import EgressMode, EgressPolicy

runner = CliRunner()
FAX_SID = "FX" + "a" * 32
RETURN_ADDRESS = {
    "name": "Jane Doe",
    "address_line1": "1 Main St",
    "address_city": "Springfield",
    "address_state": "IL",
    "address_zip": "62701",
}


def _payload(to="foia@agency.example"):
    return DeliveryPayload("FOIA Officer", to, "Records on X", "Please send the records.")


def _mail_payload():
    return DeliveryPayload(
        "FOIA Officer", "FOIA Office\n1 Agency Way\nWashington, DC 20001", "Records", "Body."
    )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFOIA_DATA_DIR", str(tmp_path / "data"))
    for var in ("OPENFOIA_TOR", "OPENFOIA_TOR_PORT", "OPENFOIA_DB_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "data"


# --- a fake Tor SOCKS5 proxy ------------------------------------------------


class FakeSocks:
    """One-connection-at-a-time SOCKS5 server on loopback.

    after_connect: "smtp" speaks SMTP to the client, "close" hangs up after
    granting the CONNECT, "refuse" answers 'not allowed by ruleset'.
    """

    def __init__(self, after_connect="close"):
        self.after_connect = after_connect
        self.requests: list[dict] = []
        self.smtp: list[bytes] = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _recv(self, conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _serve(self):
        self.srv.settimeout(0.1)  # poll, so close() does not wait on accept()
        while not self._stop:
            try:
                conn, _ = self.srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._handle(conn)
            except (ConnectionError, OSError):
                pass
            finally:
                conn.close()

    def _handle(self, conn):
        conn.settimeout(10)
        req: dict = {}
        _, n = self._recv(conn, 2)
        req["methods"] = list(self._recv(conn, n))
        method = 2 if 2 in req["methods"] else 0
        conn.sendall(bytes([5, method]))
        if method == 2:
            _, ulen = self._recv(conn, 2)
            user = self._recv(conn, ulen)
            plen = self._recv(conn, 1)[0]
            req["auth"] = (user, self._recv(conn, plen))
            conn.sendall(b"\x01\x00")
        _, cmd, _, atyp = self._recv(conn, 4)
        req["cmd"], req["atyp"] = cmd, atyp
        if atyp == 3:
            req["host"] = self._recv(conn, self._recv(conn, 1)[0]).decode()
        elif atyp == 1:
            req["host"] = socket.inet_ntoa(self._recv(conn, 4))
        else:
            req["host"] = socket.inet_ntop(socket.AF_INET6, self._recv(conn, 16))
        req["port"] = int.from_bytes(self._recv(conn, 2), "big")
        self.requests.append(req)
        if self.after_connect == "refuse":
            conn.sendall(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        conn.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        if self.after_connect == "smtp":
            self._smtp(conn)

    def _smtp(self, conn):
        f = conn.makefile("rb")
        conn.sendall(b"220 fake ESMTP\r\n")
        in_data = False
        for line in f:
            self.smtp.append(line)
            if in_data:
                if line == b".\r\n":
                    in_data = False
                    conn.sendall(b"250 queued as ABC123\r\n")
                continue
            verb = line[:4].upper()
            if verb in (b"EHLO", b"HELO"):
                conn.sendall(b"250 fake\r\n")
            elif verb == b"DATA":
                in_data = True
                conn.sendall(b"354 go ahead\r\n")
            elif verb == b"QUIT":
                conn.sendall(b"221 bye\r\n")
                return
            else:
                conn.sendall(b"250 ok\r\n")

    def close(self):
        self._stop = True
        self.srv.close()
        self.thread.join(5)


@pytest.fixture
def fake_socks():
    servers = []

    def make(after_connect="close"):
        s = FakeSocks(after_connect)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


@pytest.fixture
def no_local_dns(monkeypatch):
    """Fail if any provider or SMTP host is looked up locally (the proxy must do it)."""
    looked_up = []
    real = socket.getaddrinfo

    def guard(host, *a, **k):
        if host not in ("127.0.0.1", "localhost", b"127.0.0.1"):
            looked_up.append(host)
            raise AssertionError(f"resolved {host!r} locally")
        return real(host, *a, **k)

    def refuse(host, *a, **k):
        looked_up.append(host)
        raise AssertionError(f"resolved {host!r} locally")

    monkeypatch.setattr(socket, "getaddrinfo", guard)
    monkeypatch.setattr(socket, "gethostbyname", refuse)
    monkeypatch.setattr(socket, "gethostbyname_ex", refuse)
    return looked_up


def _tor(port, isolate=True):
    return EgressPolicy(mode=EgressMode.TOR, tor_port=port, isolate_streams=isolate)


def _never_direct_smtp(monkeypatch):
    class Direct:
        def __init__(self, *a, **k):
            raise AssertionError("SMTP connected directly in Tor mode")

    monkeypatch.setattr(email_mod.smtplib, "SMTP", Direct)


# --- SMTP through Tor -------------------------------------------------------


@pytest.mark.allow_network  # loopback only: the fake SOCKS proxy in this process
def test_smtp_goes_through_tor_by_hostname(fake_socks, no_local_dns, monkeypatch):
    proxy = fake_socks("smtp")
    _never_direct_smtp(monkeypatch)
    gw = email_mod.EmailGateway(
        smtp_host="smtp.example.org",
        smtp_port=587,
        from_email="me@example.org",
        use_tls=False,
        egress=_tor(proxy.port),
    )
    result = asyncio.run(gw.send(_payload()))
    assert result.success, result.error_message
    assert result.metadata["egress"] == "tor"
    (req,) = proxy.requests
    assert (req["cmd"], req["atyp"], req["host"], req["port"]) == (1, 3, "smtp.example.org", 587)
    user, password = req["auth"]  # stream isolation: a fresh circuit for this send
    assert user and user == password
    lines = b"".join(proxy.smtp)
    assert b"ehlo [127.0.0.1]" in lines
    assert b"rcpt to:<foia@agency.example>" in lines
    assert b"Subject: FOIA Request: Records on X" in lines
    assert no_local_dns == []


@pytest.mark.allow_network
def test_smtp_starttls_verifies_the_smtp_hostname_over_tor(fake_socks, monkeypatch):
    """STARTTLS wraps the Tor stream with server_hostname = the SMTP host."""
    proxy = fake_socks("smtp")
    seen = {}

    def fake_starttls(self, context=None):
        seen["host"] = self._host
        seen["verify"] = (context.verify_mode, context.check_hostname)
        raise email_mod.smtplib.SMTPNotSupportedError("stop here")

    monkeypatch.setattr(email_mod._TorSMTP, "starttls", fake_starttls)
    gw = email_mod.EmailGateway(smtp_host="smtp.example.org", egress=_tor(proxy.port))
    result = asyncio.run(gw.send(_payload()))
    assert not result.success
    import ssl

    assert seen == {"host": "smtp.example.org", "verify": (ssl.CERT_REQUIRED, True)}


@pytest.mark.allow_network
def test_smtp_fails_closed_when_tor_is_down(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    _never_direct_smtp(monkeypatch)
    gw = email_mod.EmailGateway(smtp_host="smtp.example.org", egress=_tor(dead_port))
    result = asyncio.run(gw.send(_payload()))
    assert result.status is DeliveryStatus.FAILED
    assert "not reachable" in result.error_message
    assert "Nothing was sent" in result.error_message


@pytest.mark.allow_network
def test_smtp_refused_by_tor_fails_and_names_the_way_out(fake_socks, monkeypatch):
    proxy = fake_socks("refuse")
    _never_direct_smtp(monkeypatch)
    gw = email_mod.EmailGateway(smtp_host="smtp.example.org", egress=_tor(proxy.port))
    result = asyncio.run(gw.send(_payload()))
    assert result.status is DeliveryStatus.FAILED
    assert "exit policy" in result.error_message
    assert "--direct" in result.error_message
    assert proxy.requests[0]["host"] == "smtp.example.org"


def test_sendgrid_is_refused_in_tor_mode():
    gw = email_mod.EmailGateway(sendgrid_api_key="SG.x", egress=_tor(9050))
    result = asyncio.run(gw.send(_payload()))
    assert result.status is DeliveryStatus.FAILED
    assert "not routed through Tor" in result.error_message


# --- Twilio and Lob through Tor (real httpx SOCKS stack) -------------------------


@pytest.mark.allow_network
def test_fax_api_goes_through_tor_by_hostname(fake_socks, no_local_dns, data_dir):
    pytest.importorskip("socksio")
    proxy = fake_socks("close")
    gw = fax_mod.TwilioFaxGateway(
        "AC123",
        "secret-token",
        "+15550000000",
        media_base_url="https://media.example.org/",
        egress=_tor(proxy.port),
    )
    result = asyncio.run(gw.send(_payload("+12025550100")))
    assert result.status is DeliveryStatus.FAILED  # the fake proxy hangs up after CONNECT
    assert "Nothing was sent directly" in result.error_message
    (req,) = proxy.requests
    assert (req["atyp"], req["host"], req["port"]) == (3, "fax.twilio.com", 443)
    assert req["auth"][0]  # stream isolation credentials
    assert no_local_dns == []


@pytest.mark.allow_network
def test_mail_api_goes_through_tor_by_hostname(fake_socks, no_local_dns):
    pytest.importorskip("socksio")
    proxy = fake_socks("close")
    gw = mail_mod.LobMailGateway("test_key", RETURN_ADDRESS, egress=_tor(proxy.port))
    result = asyncio.run(gw.send(_mail_payload()))
    assert result.status is DeliveryStatus.FAILED
    (req,) = proxy.requests
    assert (req["atyp"], req["host"], req["port"]) == (3, "api.lob.com", 443)
    assert no_local_dns == []


def test_fax_and_mail_fail_closed_without_the_socks_stack(monkeypatch, data_dir):
    """Tor configured but socksio missing: refused before any connection, never direct."""
    monkeypatch.setitem(sys.modules, "socksio", None)  # import socksio -> ImportError
    fax = fax_mod.TwilioFaxGateway(
        "AC1", "t", "+15550000000", media_base_url="https://m.example/", egress=_tor(9050)
    )
    letter = mail_mod.LobMailGateway("k", RETURN_ADDRESS, egress=_tor(9050))
    for result in (
        asyncio.run(fax.send(_payload("+12025550100"))),
        asyncio.run(letter.send(_mail_payload())),
    ):
        assert result.status is DeliveryStatus.FAILED
        assert "install-extras tor" in result.error_message
        assert "never falls back" in result.error_message


# --- the REST calls themselves (direct mode, mocked transport) -------------------


@pytest.fixture
def api(monkeypatch):
    """Route gateway API calls to a handler; returns the list of requests seen."""
    seen: list[httpx.Request] = []
    replies: list[httpx.Response] = []

    def handler(request):
        seen.append(request)
        return replies.pop(0)

    real = http_mod.egress_client

    def fake(policy=None, **kw):
        assert kw.get("follow_redirects") is False
        return real(policy, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(http_mod, "egress_client", fake)
    return seen, replies


def _basic(request) -> str:
    return base64.b64decode(request.headers["authorization"].split()[1]).decode()


def test_fax_send_calls_the_fax_api(api, data_dir):
    seen, replies = api
    replies.append(httpx.Response(201, json={"sid": FAX_SID, "status": "queued"}))
    gw = fax_mod.TwilioFaxGateway(
        "AC123", "tok", "+15550000000", media_base_url="https://media.example.org/"
    )
    result = asyncio.run(gw.send(_payload("+12025550100")))
    assert result.status is DeliveryStatus.PENDING and result.reference_id == FAX_SID
    (req,) = seen
    assert (req.method, str(req.url)) == ("POST", "https://fax.twilio.com/v1/Faxes")
    assert _basic(req) == "AC123:tok"
    form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    assert form["To"] == "+12025550100" and form["From"] == "+15550000000"
    assert form["StoreMedia"] == "false" and form["Quality"] == "fine"
    assert form["MediaUrl"].startswith("https://media.example.org/foia_fax_")
    assert "openfoia" not in req.headers["user-agent"].lower()


def test_fax_status_and_cancel(api):
    seen, replies = api
    replies += [
        httpx.Response(
            200,
            json={
                "sid": FAX_SID,
                "status": "delivered",
                "num_pages": 2,
                "price": "-0.14",
                "date_created": "2026-01-05T14:00:00Z",
                "date_updated": "2026-01-05T14:03:00Z",
            },
        ),
        httpx.Response(200, json={"sid": FAX_SID, "status": "canceled"}),
    ]
    gw = fax_mod.TwilioFaxGateway("AC123", "tok", "+15550000000")
    status = asyncio.run(gw.check_status(FAX_SID))
    assert status.status is DeliveryStatus.DELIVERED and status.cost_cents == 14
    assert status.sent_at.year == 2026 and status.metadata["num_pages"] == 2
    assert asyncio.run(gw.cancel(FAX_SID)) is True
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", f"/v1/Faxes/{FAX_SID}"),
        ("POST", f"/v1/Faxes/{FAX_SID}"),
    ]
    assert parse_qs(seen[1].content.decode()) == {"Status": ["canceled"]}


def test_fax_reference_ids_cannot_change_the_url_path(api):
    seen, _ = api
    gw = fax_mod.TwilioFaxGateway("AC123", "tok", "+15550000000")
    assert asyncio.run(gw.check_status("../../2010-04-01/Accounts")).status is DeliveryStatus.FAILED
    assert asyncio.run(gw.cancel("FX1/../x")) is False
    assert seen == []


def test_fax_api_error_is_reported_and_post_not_retried(api, data_dir):
    seen, replies = api
    replies.append(httpx.Response(503, json={"message": "Service unavailable", "code": 20503}))
    gw = fax_mod.TwilioFaxGateway("AC1", "tok", "+15550000000", media_base_url="https://m.example/")
    result = asyncio.run(gw.send(_payload("+12025550100")))
    assert result.status is DeliveryStatus.FAILED
    assert result.error_message == "Twilio API error (HTTP 503): Service unavailable"
    assert len(seen) == 1  # a fax is never sent twice by a retry


def test_mail_send_calls_the_letters_api(api):
    seen, replies = api
    replies.append(
        httpx.Response(
            200,
            json={
                "id": "ltr_abc123",
                "tracking_number": "9400",
                "expected_delivery_date": "2026-01-09",
            },
        )
    )
    gw = mail_mod.LobMailGateway("test_key", RETURN_ADDRESS)
    result = asyncio.run(gw.send(_mail_payload()))
    assert result.success and result.reference_id == "ltr_abc123"
    assert result.metadata["tracking_number"] == "9400"
    (req,) = seen
    assert (req.method, str(req.url)) == ("POST", "https://api.lob.com/v1/letters")
    assert _basic(req) == "test_key:"
    body = json.loads(req.content)
    assert body["to"]["address_city"] == "Washington" and body["from"] == RETURN_ADDRESS
    assert body["extra_service"] == "certified" and body["return_envelope"] is True
    assert body["perforated_page"] == 1 and body["color"] is False
    assert "<html>" in body["file"]


def test_mail_status_and_cancel(api):
    seen, replies = api
    replies += [
        httpx.Response(
            200,
            json={
                "id": "ltr_abc123",
                "send_date": "2026-01-05T00:00:00Z",
                "tracking_events": [
                    {"type": "certified", "name": "Mailed", "time": "2026-01-06T10:00:00Z"},
                    {"type": "certified", "name": "Delivered", "time": "2026-01-09T10:00:00Z"},
                ],
            },
        ),
        httpx.Response(200, json={"id": "ltr_abc123", "deleted": True}),
    ]
    gw = mail_mod.LobMailGateway("k", RETURN_ADDRESS)
    status = asyncio.run(gw.check_status("ltr_abc123"))
    assert status.status is DeliveryStatus.DELIVERED
    assert status.delivered_at.day == 9 and len(status.metadata["tracking_events"]) == 2
    assert asyncio.run(gw.cancel("ltr_abc123")) is True
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/v1/letters/ltr_abc123"),
        ("DELETE", "/v1/letters/ltr_abc123"),
    ]


def test_mail_api_error_message(api):
    _, replies = api
    replies.append(
        httpx.Response(
            422, json={"error": {"message": "address_zip is invalid", "status_code": 422}}
        )
    )
    result = asyncio.run(mail_mod.LobMailGateway("k", RETURN_ADDRESS).send(_mail_payload()))
    assert result.error_message == "Lob API error (HTTP 422): address_zip is invalid"


# --- request send -------------------------------------------------------------


def _send_args(method="email", to="foia@agency.example"):
    return [
        "request",
        "send",
        "-a",
        "FBI",
        "-s",
        "Records",
        "-b",
        "Please send records.",
        "-n",
        "Jane",
        "-e",
        "jane@example.org",
        "-m",
        method,
        "--to",
        to,
    ]


@pytest.fixture
def captured_sends(monkeypatch, data_dir):
    sent = []

    async def fake_send(self, payload):
        sent.append(self.egress)
        return DeliveryResult(status=DeliveryStatus.SENT, reference_id="ref-1")

    for cls in (email_mod.EmailGateway, fax_mod.TwilioFaxGateway, mail_mod.LobMailGateway):
        monkeypatch.setattr(cls, "send", fake_send)
    monkeypatch.setenv("OPENFOIA_SMTP_USER", "u")
    monkeypatch.setenv("OPENFOIA_SMTP_PASSWORD", "p")
    monkeypatch.setenv("OPENFOIA_TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("OPENFOIA_TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setenv("OPENFOIA_TWILIO_FROM_NUMBER", "+15550000000")
    monkeypatch.setenv("OPENFOIA_LOB_API_KEY", "k")
    return sent


def _tor_up(monkeypatch, up=True):
    from openfoia import net

    async def check(policy, timeout=5.0):
        return up

    monkeypatch.setattr(net, "check_tor", check)


@pytest.mark.parametrize("method", ["email", "fax"])
def test_request_send_in_tor_mode_sends_through_tor(captured_sends, monkeypatch, method):
    monkeypatch.setenv("OPENFOIA_TOR", "1")
    _tor_up(monkeypatch)
    to = "+12025550100" if method == "fax" else "foia@agency.example"
    r = runner.invoke(app_(), _send_args(method, to))
    assert r.exit_code == 0, r.output
    (egress,) = captured_sends
    assert egress.is_tor
    out = " ".join(r.output.split())
    assert "Sending through Tor" in out and "still identify you" in out


def test_request_send_tor_down_sends_nothing(captured_sends, monkeypatch):
    """Non-interactive run with Tor configured and down: exits, nothing sent, no fallback."""
    monkeypatch.setenv("OPENFOIA_TOR", "1")
    _tor_up(monkeypatch, up=False)
    r = runner.invoke(app_(), _send_args())
    assert r.exit_code == 1
    assert captured_sends == []
    assert "not reachable" in r.output


def test_request_send_direct_flag_is_explicit_and_warned(captured_sends, monkeypatch):
    monkeypatch.setenv("OPENFOIA_TOR", "1")
    _tor_up(monkeypatch, up=False)  # --direct does not need Tor
    r = runner.invoke(app_(), [*_send_args(), "--direct"])
    assert r.exit_code == 0, r.output
    (egress,) = captured_sends
    assert not egress.is_tor
    out = " ".join(r.output.split())
    assert "--direct" in out and "real IP address" in out


def test_request_send_without_tor_is_direct_and_quiet(captured_sends):
    r = runner.invoke(app_(), _send_args())
    assert r.exit_code == 0, r.output
    (egress,) = captured_sends
    assert not egress.is_tor
    assert "Tor" not in r.output


@pytest.mark.allow_network
def test_request_send_end_to_end_through_a_fake_tor(
    fake_socks, no_local_dns, data_dir, monkeypatch
):
    """The CLI, the email gateway and the SOCKS client together, no mocks in between."""
    proxy = fake_socks("smtp")
    monkeypatch.setenv("OPENFOIA_TOR", "1")
    monkeypatch.setenv("OPENFOIA_TOR_PORT", str(proxy.port))
    monkeypatch.setenv("OPENFOIA_SMTP_USER", "u")
    monkeypatch.setenv("OPENFOIA_SMTP_PASSWORD", "p")

    real_init = email_mod.EmailGateway.__init__

    def init(self, **kw):  # the fake server speaks no TLS or AUTH
        real_init(self, **{**kw, "use_tls": False, "smtp_user": None})

    monkeypatch.setattr(email_mod.EmailGateway, "__init__", init)
    r = runner.invoke(app_(), _send_args())
    assert r.exit_code == 0, r.output
    hosts = [req["host"] for req in proxy.requests]
    assert hosts[-1] == "smtp.gmail.com"  # request send's default SMTP host
    assert b"rcpt to:<foia@agency.example>" in b"".join(proxy.smtp)
    assert no_local_dns == []


def app_():
    from openfoia.cli import app

    return app
