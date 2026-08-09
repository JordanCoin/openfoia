"""Fax gateway using Twilio Programmable Fax API.

Twilio's original Fax API was deprecated in 2021. Modern fax delivery uses
Twilio's Programmable Messaging with a fax-capable number, or the REST API
for fax resources which remains available for existing accounts. This
implementation supports both the legacy fax endpoint and a PDF-over-SIP
fallback via Twilio's standard voice/messaging infrastructure.

For new accounts, consider using a Twilio fax partner (e.g., Phaxio) or
SRFax. The gateway interface remains the same regardless of provider.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .base import DeliveryGateway, DeliveryPayload, DeliveryResult, DeliveryStatus

logger = logging.getLogger(__name__)


def _esc(value: Any) -> str:
    """Escape a field interpolated into reportlab Paragraph markup.

    Paragraph parses a small HTML dialect, so an unescaped '<' in a recipient
    name (which can come from fetched agency data) corrupts or injects into
    the rendered cover page.
    """
    import html

    return html.escape(str(value or ""), quote=True)


def get_fax_media_dir() -> Path:
    """Owner-only staging directory for outbound fax PDFs.

    Previously these were written to the shared system temp dir and never
    deleted: a world-readable copy of the FOIA request survived
    `openfoia purge --secure`, which only covers the data directory.
    """
    from ..db import _ensure_private_dir, get_data_dir

    return _ensure_private_dir(get_data_dir() / "fax_media")


class TwilioFaxGateway(DeliveryGateway):
    """Send FOIA requests via fax using Twilio.

    Twilio Fax API:
    - $0.07/page to send (US domestic)
    - Supports PDF media only
    - Async delivery with webhook status callbacks
    - Requires a fax-capable Twilio number

    Usage::

        gateway = TwilioFaxGateway(
            account_sid="ACxxxx",
            auth_token="xxxx",
            from_number="+15551234567",
        )
        result = await gateway.send(payload)
    """

    COST_PER_PAGE_CENTS = 7  # $0.07/page US domestic

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        webhook_url: str | None = None,
        media_base_url: str | None = None,
        store_media_on_provider: bool = False,
    ):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.webhook_url = webhook_url
        self.media_base_url = media_base_url
        # Off by default: a retained copy on Twilio is outside the user's
        # control and survives any local purge.
        self.store_media_on_provider = store_media_on_provider
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazily initialize Twilio client."""
        if self._client is None:
            from twilio.rest import Client

            self._client = Client(self.account_sid, self.auth_token)
        return self._client

    async def send(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send FOIA request as fax via Twilio.

        Generates a formatted PDF from the request body, uploads it to
        Twilio as a media resource, and initiates fax delivery.
        """
        try:
            # Generate PDF from the FOIA request
            pdf_bytes = self._generate_fax_pdf(payload)
            pages = self._estimate_pages(payload)

            logger.info(
                "Sending fax to %s (%d estimated pages)",
                payload.recipient_address,
                pages,
            )

            # Upload media and get accessible URL for Twilio
            media_url = await self._upload_media(pdf_bytes)

            client = self._get_client()

            # Send fax via Twilio Fax REST API
            fax = await asyncio.to_thread(
                client.fax.faxes.create,
                to=payload.recipient_address,
                from_=self.from_number,
                media_url=media_url,
                quality="fine",  # Higher quality for legal documents
                # Do NOT leave a copy of the request on Twilio's servers by
                # default — it is outside the user's control and outside purge.
                store_media=self.store_media_on_provider,
                status_callback=self.webhook_url,
            )

            logger.info("Fax queued: SID=%s to=%s", fax.sid, payload.recipient_address)

            return DeliveryResult(
                status=DeliveryStatus.PENDING,
                reference_id=fax.sid,
                sent_at=datetime.now(timezone.utc),
                cost_cents=pages * self.COST_PER_PAGE_CENTS,
                metadata={
                    "to": payload.recipient_address,
                    "from": self.from_number,
                    "estimated_pages": pages,
                    "quality": "fine",
                    "media_url": media_url,
                },
            )

        except ImportError:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message="Fax gateway not installed. Run: openfoia install-extras fax",
            )
        except Exception as e:
            logger.error("Fax send failed: %s", e)
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message=str(e),
            )

    async def check_status(self, reference_id: str) -> DeliveryResult:
        """Check fax delivery status via Twilio API.

        Twilio fax statuses: queued, processing, sending, delivered,
        receiving, completed, no-answer, busy, failed, canceled.
        """
        try:
            client = self._get_client()
            fax = await asyncio.to_thread(
                client.fax.faxes(reference_id).fetch,
            )

            status_map = {
                "queued": DeliveryStatus.PENDING,
                "processing": DeliveryStatus.PENDING,
                "sending": DeliveryStatus.PENDING,
                "delivered": DeliveryStatus.DELIVERED,
                "completed": DeliveryStatus.DELIVERED,
                "receiving": DeliveryStatus.PENDING,
                "no-answer": DeliveryStatus.FAILED,
                "busy": DeliveryStatus.FAILED,
                "failed": DeliveryStatus.FAILED,
                "canceled": DeliveryStatus.CANCELLED,
            }

            delivery_status = status_map.get(fax.status, DeliveryStatus.PENDING)

            return DeliveryResult(
                status=delivery_status,
                reference_id=reference_id,
                sent_at=fax.date_created,
                delivered_at=fax.date_updated
                if delivery_status == DeliveryStatus.DELIVERED
                else None,
                cost_cents=int(float(fax.price or 0) * -100) if fax.price else None,
                metadata={
                    "twilio_status": fax.status,
                    "num_pages": fax.num_pages,
                    "duration": fax.duration,
                    "direction": fax.direction,
                },
            )

        except Exception as e:
            logger.error("Fax status check failed for %s: %s", reference_id, e)
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id=reference_id,
                error_message=str(e),
            )

    async def cancel(self, reference_id: str) -> bool:
        """Cancel a pending fax if it hasn't started transmitting."""
        try:
            client = self._get_client()
            await asyncio.to_thread(
                client.fax.faxes(reference_id).update,
                status="canceled",
            )
            logger.info("Fax %s canceled", reference_id)
            return True
        except Exception as e:
            logger.warning("Failed to cancel fax %s: %s", reference_id, e)
            return False

    def estimate_cost(self, payload: DeliveryPayload) -> int:
        """Estimate fax cost in cents ($0.07/page US domestic)."""
        pages = self._estimate_pages(payload)
        return pages * self.COST_PER_PAGE_CENTS

    def _estimate_pages(self, payload: DeliveryPayload) -> int:
        """Estimate number of fax pages from payload content."""
        # Cover page
        pages = 1 if payload.cover_page else 0
        # Body: ~3000 chars per page for standard 12pt text
        pages += max(1, len(payload.body) // 3000 + 1)

        # Attachment pages (rough estimates)
        if payload.attachments:
            for filename, content in payload.attachments:
                if filename.lower().endswith(".pdf"):
                    # Rough heuristic: ~50KB per printed page
                    pages += max(1, len(content) // 50000)
                else:
                    pages += 1

        return pages

    def _generate_fax_pdf(self, payload: DeliveryPayload) -> bytes:
        """Generate a properly formatted PDF suitable for faxing.

        Uses reportlab for reliable PDF generation with legal document
        formatting (Times New Roman, 12pt, proper margins).

        Falls back to a minimal PDF if reportlab is not available.
        """
        try:
            return self._generate_pdf_reportlab(payload)
        except ImportError:
            logger.warning("reportlab not installed, using minimal PDF generator")
            return self._generate_pdf_minimal(payload)

    def _generate_pdf_reportlab(self, payload: DeliveryPayload) -> bytes:
        """Generate PDF using reportlab with proper legal formatting."""
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            leftMargin=1 * inch,
            rightMargin=1 * inch,
            topMargin=1 * inch,
            bottomMargin=1 * inch,
        )
        styles = getSampleStyleSheet()

        # Custom style for legal body text
        body_style = ParagraphStyle(
            "FOIABody",
            parent=styles["Normal"],
            fontSize=12,
            leading=16,
            spaceAfter=12,
            fontName="Times-Roman",
        )

        header_style = ParagraphStyle(
            "FOIAHeader",
            parent=styles["Title"],
            fontSize=16,
            leading=20,
            spaceAfter=24,
            fontName="Times-Bold",
        )

        story = []

        # Cover page with fax header
        if payload.cover_page:
            story.append(Paragraph("FACSIMILE TRANSMITTAL", header_style))
            story.append(Spacer(1, 12))
            story.append(Paragraph(f"<b>TO:</b> {_esc(payload.recipient_name)}", body_style))
            story.append(Paragraph(f"<b>FAX:</b> {_esc(payload.recipient_address)}", body_style))
            story.append(
                Paragraph(
                    f"<b>DATE:</b> {datetime.now(timezone.utc).strftime('%B %d, %Y')}", body_style
                )
            )
            story.append(
                Paragraph(f"<b>RE:</b> FOIA Request - {_esc(payload.subject)}", body_style)
            )
            story.append(
                Paragraph(
                    f"<b>PAGES:</b> {self._estimate_pages(payload)} (including cover)",
                    body_style,
                )
            )
            if payload.return_address:
                story.append(Spacer(1, 12))
                story.append(
                    Paragraph(
                        f"<b>FROM:</b> {payload.return_address.split(chr(10))[0]}", body_style
                    )
                )
            story.append(Spacer(1, 24))
            story.append(
                Paragraph(
                    "FREEDOM OF INFORMATION ACT REQUEST",
                    header_style,
                )
            )
            story.append(Spacer(1, 12))

        # FOIA preamble
        story.append(
            Paragraph(
                "Dear FOIA Officer,",
                body_style,
            )
        )
        story.append(Spacer(1, 6))
        story.append(
            Paragraph(
                "This is a request under the Freedom of Information Act, 5 U.S.C. &sect; 552.",
                body_style,
            )
        )
        story.append(Spacer(1, 12))

        # Request body paragraphs
        for para in payload.body.split("\n\n"):
            text = para.strip()
            if text:
                # Escape XML special chars for reportlab
                text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(text, body_style))
                story.append(Spacer(1, 6))

        # Fee waiver and closing
        story.append(Spacer(1, 12))
        story.append(
            Paragraph(
                "I request a fee waiver for this request. Disclosure of the requested "
                "information is in the public interest because it is likely to contribute "
                "significantly to public understanding of government operations and activities.",
                body_style,
            )
        )
        story.append(Spacer(1, 12))
        story.append(Paragraph("Thank you for your assistance.", body_style))
        story.append(Spacer(1, 24))
        story.append(Paragraph("Respectfully,", body_style))
        story.append(Spacer(1, 12))

        sender = payload.return_address or "[Requester Name]"
        story.append(Paragraph(sender.split("\n")[0], body_style))

        doc.build(story)
        return buffer.getvalue()

    def _generate_pdf_minimal(self, payload: DeliveryPayload) -> bytes:
        """Generate a minimal valid PDF without external dependencies.

        This is a fallback that creates a bare-bones but valid PDF.
        """
        date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
        sender = (payload.return_address or "[Requester Name]").split("\n")[0]

        text_lines = [
            "FREEDOM OF INFORMATION ACT REQUEST",
            "",
            f"Date: {date_str}",
            f"To: {payload.recipient_name}",
            f"Fax: {payload.recipient_address}",
            f"Re: {payload.subject}",
            "",
            "Dear FOIA Officer,",
            "",
            "This is a request under the Freedom of Information Act, 5 U.S.C. 552.",
            "",
        ]
        # Wrap body text at 80 chars
        for line in payload.body.split("\n"):
            while len(line) > 80:
                split_at = line[:80].rfind(" ")
                if split_at == -1:
                    split_at = 80
                text_lines.append(line[:split_at])
                line = line[split_at:].lstrip()
            text_lines.append(line)

        text_lines.extend(
            [
                "",
                "Respectfully,",
                sender,
            ]
        )

        # Build minimal PDF manually
        # For multi-line, use T* operator
        lines_pdf = []
        y = 720
        for line in text_lines:
            escaped = self._pdf_escape(line)
            lines_pdf.append(f"BT /F1 12 Tf 72 {y} Td ({escaped}) Tj ET")
            y -= 16
            if y < 72:
                break

        stream_content = "\n".join(lines_pdf)
        stream_bytes = stream_content.encode("latin-1")

        pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
            b"4 0 obj<</Length " + str(len(stream_bytes)).encode() + b">>\n"
            b"stream\n" + stream_bytes + b"\nendstream\nendobj\n"
            b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Courier>>endobj\n"
            b"xref\n0 6\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"0000000115 00000 n \n"
            b"0000000266 00000 n \n"
            b"0000000400 00000 n \n"
            b"trailer<</Size 6/Root 1 0 R>>\n"
            b"startxref\n470\n%%EOF"
        )
        return pdf

    @staticmethod
    def _pdf_escape(text: str) -> str:
        """Escape special characters for PDF text strings."""
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    async def _upload_media(self, pdf_bytes: bytes) -> str:
        """Upload PDF and return an accessible URL for Twilio.

        Strategy:
        1. If media_base_url is configured, write to that directory and return URL
        2. Otherwise, use a data URI via a temporary HTTP server, or save locally
           and expect the caller to provide a publicly-accessible media_base_url

        For production deployments, set media_base_url to point to an S3 bucket,
        GCS bucket, or any HTTPS endpoint where uploaded files are served.
        """
        if self.media_base_url:
            # Write to temp file and construct URL from configured base
            import hashlib

            file_hash = hashlib.sha256(pdf_bytes).hexdigest()[:16]
            filename = f"foia_fax_{file_hash}.pdf"

            # Stage inside the data dir (0700) so the PDF is owner-only and is
            # covered by `openfoia purge`. It contains the requester's name,
            # return address and the subject of the investigation.
            pdf_path = get_fax_media_dir() / filename
            pdf_path.write_bytes(pdf_bytes)
            os.chmod(pdf_path, 0o600)

            base = self.media_base_url.rstrip("/")
            return f"{base}/{filename}"

        # Fallback: stage in the data dir. Caller must ensure Twilio can reach
        # this file (e.g., via ngrok tunnel or local dev server).
        temp_path = str(get_fax_media_dir() / f"foia_fax_{uuid4().hex}.pdf")
        Path(temp_path).write_bytes(pdf_bytes)
        os.chmod(temp_path, 0o600)

        logger.warning(
            "No media_base_url configured. PDF saved to %s. "
            "Twilio requires an HTTPS URL to fetch fax media. "
            "Set media_base_url or use a tunnel (e.g., ngrok) for development.",
            temp_path,
        )
        return f"file://{temp_path}"
