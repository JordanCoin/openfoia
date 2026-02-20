"""Fax gateway using Twilio Programmable Fax API."""

from __future__ import annotations

import asyncio
import io
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import DeliveryGateway, DeliveryPayload, DeliveryResult, DeliveryStatus


class TwilioFaxGateway(DeliveryGateway):
    """Send FOIA requests via fax using Twilio.
    
    Twilio Fax API:
    - $0.07/page to send (US)
    - Supports PDF only
    - Async delivery with webhook callbacks
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        webhook_url: str | None = None,
    ):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.webhook_url = webhook_url
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from twilio.rest import Client
            self._client = Client(self.account_sid, self.auth_token)
        return self._client

    async def send(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send fax via Twilio.
        
        Twilio requires media to be accessible via URL, so we either:
        1. Upload to temp storage and provide URL
        2. Use Twilio's media hosting
        
        For simplicity, we'll generate a PDF and use Twilio's media upload.
        """
        # Generate PDF from payload
        pdf_bytes = self._generate_fax_pdf(payload)
        
        # Upload to temp storage (in production, use S3/GCS/etc)
        # For now, we'll use Twilio's built-in media hosting
        media_url = await self._upload_media(pdf_bytes)
        
        try:
            client = self._get_client()
            
            # Send fax asynchronously
            fax = await asyncio.to_thread(
                client.fax.faxes.create,
                to=payload.recipient_address,
                from_=self.from_number,
                media_url=media_url,
                status_callback=self.webhook_url,
            )
            
            return DeliveryResult(
                status=DeliveryStatus.PENDING,
                reference_id=fax.sid,
                sent_at=datetime.utcnow(),
                metadata={
                    "to": payload.recipient_address,
                    "from": self.from_number,
                    "pages": self._estimate_pages(payload),
                },
            )
            
        except Exception as e:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message=str(e),
            )

    async def check_status(self, reference_id: str) -> DeliveryResult:
        """Check fax delivery status."""
        try:
            client = self._get_client()
            fax = await asyncio.to_thread(
                client.fax.faxes.get,
                reference_id,
            )
            
            status_map = {
                "queued": DeliveryStatus.PENDING,
                "processing": DeliveryStatus.PENDING,
                "sending": DeliveryStatus.PENDING,
                "delivered": DeliveryStatus.DELIVERED,
                "no-answer": DeliveryStatus.FAILED,
                "busy": DeliveryStatus.FAILED,
                "failed": DeliveryStatus.FAILED,
                "canceled": DeliveryStatus.CANCELLED,
            }
            
            return DeliveryResult(
                status=status_map.get(fax.status, DeliveryStatus.PENDING),
                reference_id=reference_id,
                sent_at=fax.date_created,
                delivered_at=fax.date_updated if fax.status == "delivered" else None,
                error_message=fax.error_message if hasattr(fax, 'error_message') else None,
                cost_cents=int(float(fax.price or 0) * -100) if fax.price else None,
                metadata={
                    "status": fax.status,
                    "pages": fax.num_pages,
                    "duration": fax.duration,
                },
            )
            
        except Exception as e:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id=reference_id,
                error_message=str(e),
            )

    async def cancel(self, reference_id: str) -> bool:
        """Cancel a pending fax."""
        try:
            client = self._get_client()
            await asyncio.to_thread(
                client.fax.faxes.get(reference_id).update,
                status="canceled",
            )
            return True
        except Exception:
            return False

    def estimate_cost(self, payload: DeliveryPayload) -> int:
        """Estimate fax cost in cents ($0.07/page)."""
        pages = self._estimate_pages(payload)
        return pages * 7

    def _estimate_pages(self, payload: DeliveryPayload) -> int:
        """Estimate number of fax pages."""
        # Cover page + ~3000 chars per page for body
        pages = 1 if payload.cover_page else 0
        pages += max(1, len(payload.body) // 3000 + 1)
        
        # Add attachment pages (rough estimate)
        if payload.attachments:
            for filename, content in payload.attachments:
                if filename.endswith(".pdf"):
                    # Rough: 50KB per page
                    pages += max(1, len(content) // 50000)
                else:
                    pages += 1
        
        return pages

    def _generate_fax_pdf(self, payload: DeliveryPayload) -> bytes:
        """Generate a PDF suitable for faxing.
        
        In production, use reportlab or weasyprint for proper formatting.
        """
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        styles = getSampleStyleSheet()
        story = []
        
        # Cover page
        if payload.cover_page:
            story.append(Paragraph("FREEDOM OF INFORMATION ACT REQUEST", styles['Title']))
            story.append(Spacer(1, 20))
            story.append(Paragraph(f"TO: {payload.recipient_name}", styles['Normal']))
            story.append(Paragraph(f"FAX: {payload.recipient_address}", styles['Normal']))
            story.append(Spacer(1, 10))
            story.append(Paragraph(f"RE: {payload.subject}", styles['Heading2']))
            story.append(Spacer(1, 20))
        
        # Body
        for para in payload.body.split('\n\n'):
            if para.strip():
                story.append(Paragraph(para, styles['Normal']))
                story.append(Spacer(1, 10))
        
        doc.build(story)
        return buffer.getvalue()

    async def _upload_media(self, pdf_bytes: bytes) -> str:
        """Upload PDF to accessible URL.
        
        In production, upload to S3/GCS with a signed URL.
        For development, save locally and serve via ngrok or similar.
        """
        # For now, save to temp file and return file:// URL
        # In production: upload to cloud storage
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            temp_path = f.name
        
        # TODO: In production, upload to S3 and return signed URL
        # For now, this requires a separate file server
        return f"file://{temp_path}"
