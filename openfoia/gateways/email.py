"""Email gateway for FOIA requests."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from .base import DeliveryGateway, DeliveryPayload, DeliveryResult, DeliveryStatus


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
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_email = from_email or smtp_user
        self.from_name = from_name
        self.use_tls = use_tls
        self.sendgrid_api_key = sendgrid_api_key

    async def send(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send FOIA request via email."""
        if self.sendgrid_api_key:
            return await self._send_sendgrid(payload)
        else:
            return await self._send_smtp(payload)

    async def _send_smtp(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send via SMTP."""
        try:
            # Build message
            msg = MIMEMultipart()
            msg['From'] = f"{self.from_name} <{self.from_email}>"
            msg['To'] = payload.recipient_address
            msg['Subject'] = f"FOIA Request: {payload.subject}"
            
            # Request read receipt
            msg['Disposition-Notification-To'] = self.from_email
            msg['Return-Receipt-To'] = self.from_email
            
            # Body
            body = self._format_email_body(payload)
            msg.attach(MIMEText(body, 'plain'))
            
            # Attachments
            if payload.attachments:
                for filename, content in payload.attachments:
                    part = MIMEApplication(content, Name=filename)
                    part['Content-Disposition'] = f'attachment; filename="{filename}"'
                    msg.attach(part)
            
            # Send
            def _send():
                context = ssl.create_default_context()
                with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                    if self.use_tls:
                        server.starttls(context=context)
                    if self.smtp_user and self.smtp_password:
                        server.login(self.smtp_user, self.smtp_password)
                    server.send_message(msg)
            
            await asyncio.to_thread(_send)
            
            # Generate a reference ID (email doesn't have built-in tracking)
            import hashlib
            ref_id = hashlib.sha256(
                f"{payload.recipient_address}:{payload.subject}:{datetime.utcnow().isoformat()}".encode()
            ).hexdigest()[:16]
            
            return DeliveryResult(
                status=DeliveryStatus.SENT,
                reference_id=ref_id,
                sent_at=datetime.utcnow(),
                cost_cents=0,  # Email is free (sort of)
                metadata={
                    "to": payload.recipient_address,
                    "from": self.from_email,
                    "subject": msg['Subject'],
                    "method": "smtp",
                },
            )
            
        except Exception as e:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message=str(e),
            )

    async def _send_sendgrid(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send via SendGrid API."""
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import (
                Mail, Attachment, FileContent, FileName, FileType, Disposition
            )
            import base64
            
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
            message_id = response.headers.get('X-Message-Id', '')
            
            return DeliveryResult(
                status=DeliveryStatus.SENT,
                reference_id=message_id,
                sent_at=datetime.utcnow(),
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
Date: {datetime.utcnow().strftime('%B %d, %Y')}

I request a fee waiver for this request. Disclosure of the requested information is in the public interest because it is likely to contribute significantly to public understanding of government operations and activities.

If you have any questions about this request, please contact me at this email address.

Thank you for your assistance.

Respectfully,
{payload.return_address or '[Requester Name]'}
"""
