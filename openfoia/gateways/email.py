"""Email gateway for FOIA requests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import getaddresses

from ..models import utcnow as _utcnow
from ..net import EgressPolicy, TorUnavailableError, open_tor_stream
from .base import DeliveryGateway, DeliveryPayload, DeliveryResult, DeliveryStatus

#: The name sent in EHLO. smtplib's default is socket.getfqdn(): the machine's
#: hostname ("janes-macbook-pro.local"), which the mail server logs and often
#: copies into a Received: header the agency reads. An address literal is
#: valid EHLO syntax (RFC 5321) and is what many mail clients send.
EHLO_NAME = "[127.0.0.1]"

#: Seconds for each SMTP socket operation over Tor (circuits are slow to build).
TOR_SMTP_TIMEOUT = 90.0

#: Added to a failed SMTP send over Tor: the usual reasons, and the way out.
TOR_SMTP_HINT = (
    "Sent through Tor, so this may be your mail provider refusing Tor exit "
    "addresses (many do), or the Tor exit refusing the port. Nothing was sent "
    "directly. To send this one request without Tor, run again with --direct "
    "(your mail server then sees your real IP address)."
)


class _TorSMTP(smtplib.SMTP):
    """smtplib.SMTP whose socket is a stream through the Tor SOCKS proxy.

    The SMTP host name is resolved at the exit relay (open_tor_stream sends it
    as a SOCKS5 DOMAINNAME). STARTTLS still verifies the server certificate
    against that host name: smtplib wraps the socket with server_hostname set
    to the host passed here.
    """

    def __init__(self, host: str, port: int, *, policy: EgressPolicy, **kwargs):
        self._policy = policy  # before super().__init__, which connects
        super().__init__(host, port, **kwargs)

    def _get_socket(self, host, port, timeout):
        return open_tor_stream(self._policy, host, port, timeout=timeout)


def validate_single_recipient(address: str) -> str:
    """Return *address* if it is exactly one plain email address.

    ``smtplib.send_message`` derives the envelope recipients from the To/Cc/Bcc
    headers, so ``agency@gov, attacker@evil`` silently delivers a copy of the
    FOIA request — the requester's identity and the subject of their
    investigation — to the attacker. No CRLF is needed; a comma is enough.
    """
    if not address or not address.strip():
        raise ValueError("No recipient address provided.")

    if any(ch in address for ch in "\r\n"):
        raise ValueError("Invalid recipient address: contains a line break.")

    parsed = getaddresses([address])
    if len(parsed) != 1:
        raise ValueError(
            f"Refusing to send: {len(parsed)} recipient addresses supplied, expected exactly one. "
            "A second address would receive a copy of this request."
        )

    _, addr = parsed[0]
    if not addr or addr.count("@") != 1 or addr.startswith("@") or addr.endswith("@"):
        raise ValueError(f"Invalid recipient address: {address!r}")

    return addr


class EmailGateway(DeliveryGateway):
    """Send FOIA requests via email.

    Supports:
    - Direct SMTP
    - SendGrid API
    - AWS SES

    Email is preferred by many agencies and provides:
    - Instant delivery
    - Automatic read receipts (sometimes)
    - Easy attachment handling
    - Paper trail via sent folder

    With a Tor *egress* policy, SMTP goes through the Tor SOCKS proxy (see
    _TorSMTP) and fails closed: if Tor is not reachable or refuses the
    connection the send fails, it is never retried directly. SendGrid is not
    routed through Tor and is refused in Tor mode. The EHLO greeting does not
    carry this machine's hostname (EHLO_NAME).
    """

    def __init__(
        self,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 587,
        smtp_user: str | None = None,
        smtp_password: str | None = None,
        from_email: str | None = None,
        from_name: str = "FOIA Requester",
        use_tls: bool = True,
        # Alternative: SendGrid
        sendgrid_api_key: str | None = None,
        egress: EgressPolicy | None = None,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_email = from_email or smtp_user
        self.from_name = from_name
        self.use_tls = use_tls
        self.sendgrid_api_key = sendgrid_api_key
        self.egress = egress if egress is not None else EgressPolicy()

    async def send(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send FOIA request via email."""
        try:
            validate_single_recipient(payload.recipient_address)
        except ValueError as e:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message=str(e),
            )

        if self.sendgrid_api_key:
            if self.egress.is_tor:
                return DeliveryResult(
                    status=DeliveryStatus.FAILED,
                    reference_id="",
                    error_message=(
                        "SendGrid is not routed through Tor, so it is not used in Tor "
                        "mode. Nothing was sent. Use SMTP, or run again with --direct."
                    ),
                )
            return await self._send_sendgrid(payload)
        else:
            return await self._send_smtp(payload)

    async def _send_smtp(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send via SMTP."""
        try:
            recipient = validate_single_recipient(payload.recipient_address)

            # Build message
            msg = MIMEMultipart()
            msg["From"] = f"{self.from_name} <{self.from_email}>"
            msg["To"] = recipient
            msg["Subject"] = f"FOIA Request: {payload.subject}"

            # Request read receipt
            msg["Disposition-Notification-To"] = self.from_email
            msg["Return-Receipt-To"] = self.from_email

            # Body
            body = self._format_email_body(payload)
            msg.attach(MIMEText(body, "plain"))

            # Attachments
            if payload.attachments:
                for filename, content in payload.attachments:
                    part = MIMEApplication(content, Name=filename)
                    part["Content-Disposition"] = f'attachment; filename="{filename}"'
                    msg.attach(part)

            # Send
            def _connect() -> smtplib.SMTP:
                if self.egress.is_tor:
                    return _TorSMTP(
                        self.smtp_host,
                        self.smtp_port,
                        policy=self.egress,
                        local_hostname=EHLO_NAME,
                        timeout=TOR_SMTP_TIMEOUT,
                    )
                return smtplib.SMTP(self.smtp_host, self.smtp_port, local_hostname=EHLO_NAME)

            def _send():
                context = ssl.create_default_context()
                with _connect() as server:
                    if self.use_tls:
                        server.starttls(context=context)
                    if self.smtp_user and self.smtp_password:
                        server.login(self.smtp_user, self.smtp_password)
                    # Pass the envelope explicitly rather than letting smtplib
                    # re-derive it from the headers.
                    server.send_message(msg, to_addrs=[recipient])

            await asyncio.to_thread(_send)

            # Generate a reference ID (email doesn't have built-in tracking)
            ref_id = hashlib.sha256(
                f"{payload.recipient_address}:{payload.subject}:{_utcnow().isoformat()}".encode()
            ).hexdigest()[:16]

            return DeliveryResult(
                status=DeliveryStatus.SENT,
                reference_id=ref_id,
                sent_at=_utcnow(),
                cost_cents=0,  # Email is free (sort of)
                metadata={
                    "to": payload.recipient_address,
                    "from": self.from_email,
                    "subject": msg["Subject"],
                    "method": "smtp",
                    "egress": "tor" if self.egress.is_tor else "direct",
                },
            )

        except TorUnavailableError as e:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message=f"{e}. Nothing was sent; start Tor or run again with --direct.",
            )
        except Exception as e:
            message = str(e)
            if self.egress.is_tor:
                message = f"{message} ({TOR_SMTP_HINT})"
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message=message,
            )

    async def _send_sendgrid(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send via SendGrid API."""
        try:
            from sendgrid import SendGridAPIClient  # optional: sendgrid (no extra)
            from sendgrid.helpers.mail import (  # optional: sendgrid (no extra)
                Attachment,
                Disposition,
                FileContent,
                FileName,
                FileType,
                Mail,
            )

            message = Mail(
                from_email=(self.from_email, self.from_name),
                to_emails=payload.recipient_address,
                subject=f"FOIA Request: {payload.subject}",
                plain_text_content=self._format_email_body(payload),
            )

            # Attachments
            if payload.attachments:
                for filename, content in payload.attachments:
                    encoded = base64.b64encode(content).decode()
                    attachment = Attachment(
                        FileContent(encoded),
                        FileName(filename),
                        FileType("application/octet-stream"),
                        Disposition("attachment"),
                    )
                    message.add_attachment(attachment)

            sg = SendGridAPIClient(self.sendgrid_api_key)
            response = await asyncio.to_thread(
                sg.send,
                message,
            )

            # SendGrid returns message ID in headers
            message_id = response.headers.get("X-Message-Id", "")

            return DeliveryResult(
                status=DeliveryStatus.SENT,
                reference_id=message_id,
                sent_at=_utcnow(),
                cost_cents=0,
                metadata={
                    "to": payload.recipient_address,
                    "from": self.from_email,
                    "status_code": response.status_code,
                    "method": "sendgrid",
                },
            )

        except Exception as e:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message=str(e),
            )

    async def check_status(self, reference_id: str) -> DeliveryResult:
        """Check email status.

        Email doesn't have built-in delivery tracking (unlike fax/mail).
        With SendGrid, we can check via their Events API if webhooks are set up.
        """
        # For SMTP, we can't really track
        # For SendGrid, would need webhook setup
        return DeliveryResult(
            status=DeliveryStatus.SENT,  # Assume sent = delivered for email
            reference_id=reference_id,
            metadata={"note": "Email delivery tracking requires webhook setup"},
        )

    async def cancel(self, reference_id: str) -> bool:
        """Can't cancel a sent email."""
        return False

    def estimate_cost(self, payload: DeliveryPayload) -> int:
        """Email is essentially free."""
        return 0

    def _format_email_body(self, payload: DeliveryPayload) -> str:
        """Format the FOIA request as email body text."""
        return f"""Dear FOIA Officer,

This is a request under the Freedom of Information Act, 5 U.S.C. § 552.

{payload.body}

---
REQUEST DETAILS
Subject: {payload.subject}
Date: {_utcnow().strftime("%B %d, %Y")}

I request a fee waiver for this request. Disclosure of the requested information is in the public interest because it is likely to contribute significantly to public understanding of government operations and activities.

If you have any questions about this request, please contact me at this email address.

Thank you for your assistance.

Respectfully,
{payload.return_address or "[Requester Name]"}
"""
