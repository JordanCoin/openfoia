"""Physical mail gateway using Lob API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from .base import DeliveryGateway, DeliveryPayload, DeliveryResult, DeliveryStatus


class LobMailGateway(DeliveryGateway):
    """Send FOIA requests via physical mail using Lob.
    
    Lob API:
    - Letters start at $0.63 (First Class)
    - Certified mail available
    - Tracking included
    - 3-5 business days delivery
    """

    def __init__(
        self,
        api_key: str,
        return_address: dict[str, str],
        use_certified: bool = True,
    ):
        self.api_key = api_key
        self.return_address = return_address
        self.use_certified = use_certified
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import lob
            lob.api_key = self.api_key
            self._client = lob
        return self._client

    async def send(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send physical letter via Lob."""
        try:
            lob = self._get_client()
            
            # Parse address (expecting structured format)
            to_address = self._parse_address(payload.recipient_address)
            
            # Generate letter HTML
            letter_html = self._generate_letter_html(payload)
            
            # Create letter
            letter = await asyncio.to_thread(
                lob.Letter.create,
                description=f"FOIA Request: {payload.subject[:50]}",
                to_address=to_address,
                from_address=self.return_address,
                file=letter_html,
                color=False,
                mail_type="usps_first_class",
                extra_service="certified" if self.use_certified else None,
                return_envelope=True,  # Include return envelope for response
            )
            
            return DeliveryResult(
                status=DeliveryStatus.SENT,
                reference_id=letter.id,
                sent_at=datetime.utcnow(),
                cost_cents=letter.price_in_cents if hasattr(letter, 'price_in_cents') else None,
                metadata={
                    "expected_delivery_date": letter.expected_delivery_date,
                    "carrier": "USPS",
                    "mail_type": "certified" if self.use_certified else "first_class",
                    "tracking_number": letter.tracking_number if hasattr(letter, 'tracking_number') else None,
                },
            )
            
        except Exception as e:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message=str(e),
            )

    async def check_status(self, reference_id: str) -> DeliveryResult:
        """Check letter delivery status."""
        try:
            lob = self._get_client()
            letter = await asyncio.to_thread(
                lob.Letter.retrieve,
                reference_id,
            )
            
            # Map Lob tracking events to our status
            status = DeliveryStatus.SENT
            delivered_at = None
            
            if hasattr(letter, 'tracking_events') and letter.tracking_events:
                latest_event = letter.tracking_events[-1]
                if latest_event.type == "delivered":
                    status = DeliveryStatus.DELIVERED
                    delivered_at = latest_event.time
                elif latest_event.type in ("returned", "re-routed"):
                    status = DeliveryStatus.FAILED
            
            return DeliveryResult(
                status=status,
                reference_id=reference_id,
                sent_at=letter.send_date,
                delivered_at=delivered_at,
                cost_cents=letter.price_in_cents if hasattr(letter, 'price_in_cents') else None,
                metadata={
                    "tracking_events": [
                        {"type": e.type, "time": e.time, "location": e.location}
                        for e in (letter.tracking_events or [])
                    ] if hasattr(letter, 'tracking_events') else [],
                },
            )
            
        except Exception as e:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id=reference_id,
                error_message=str(e),
            )

    async def cancel(self, reference_id: str) -> bool:
        """Cancel a letter (only works before it's printed)."""
        try:
            lob = self._get_client()
            await asyncio.to_thread(
                lob.Letter.delete,
                reference_id,
            )
            return True
        except Exception:
            return False

    def estimate_cost(self, payload: DeliveryPayload) -> int:
        """Estimate mailing cost in cents.
        
        Base costs (approximate):
        - First class letter: $0.63-$1.20 depending on pages
        - Certified mail: +$4.00
        - Return receipt: +$3.00
        """
        pages = self._estimate_pages(payload)
        
        # Base cost: $0.63 for 1 page, +$0.15 per additional page
        cost = 63 + max(0, (pages - 1) * 15)
        
        # Certified mail
        if self.use_certified:
            cost += 400
        
        # Return envelope (we include this)
        cost += 50
        
        return cost

    def _estimate_pages(self, payload: DeliveryPayload) -> int:
        """Estimate number of pages."""
        # ~3000 characters per page
        pages = max(1, len(payload.body) // 3000 + 1)
        
        if payload.attachments:
            for filename, content in payload.attachments:
                pages += max(1, len(content) // 3000 + 1)
        
        return pages

    def _parse_address(self, address_str: str) -> dict[str, str]:
        """Parse address string into Lob format.
        
        Expects format:
        Name
        Address Line 1
        Address Line 2 (optional)
        City, State ZIP
        """
        lines = [l.strip() for l in address_str.strip().split('\n') if l.strip()]
        
        if len(lines) < 3:
            raise ValueError(f"Invalid address format: {address_str}")
        
        name = lines[0]
        address_line_1 = lines[1]
        address_line_2 = lines[2] if len(lines) > 3 else None
        city_state_zip = lines[-1]
        
        # Parse "City, State ZIP"
        import re
        match = re.match(r'^(.+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$', city_state_zip)
        if not match:
            raise ValueError(f"Invalid city/state/zip: {city_state_zip}")
        
        city, state, zip_code = match.groups()
        
        result = {
            "name": name,
            "address_line1": address_line_1,
            "address_city": city,
            "address_state": state,
            "address_zip": zip_code,
            "address_country": "US",
        }
        
        if address_line_2:
            result["address_line2"] = address_line_2
        
        return result

    def _generate_letter_html(self, payload: DeliveryPayload) -> str:
        """Generate formatted letter HTML for Lob."""
        return f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{
            font-family: 'Times New Roman', serif;
            font-size: 12pt;
            line-height: 1.5;
            margin: 1in;
        }}
        .header {{
            margin-bottom: 0.5in;
        }}
        .date {{
            margin-bottom: 0.25in;
        }}
        .recipient {{
            margin-bottom: 0.5in;
        }}
        .subject {{
            font-weight: bold;
            margin-bottom: 0.25in;
        }}
        .body {{
            text-align: justify;
        }}
        .body p {{
            margin-bottom: 0.15in;
        }}
        .signature {{
            margin-top: 0.5in;
        }}
    </style>
</head>
<body>
    <div class="header">
        <strong>FREEDOM OF INFORMATION ACT REQUEST</strong>
    </div>
    
    <div class="date">
        {{{{DATE}}}}
    </div>
    
    <div class="recipient">
        {payload.recipient_name}<br>
        {payload.recipient_address.replace(chr(10), '<br>')}
    </div>
    
    <div class="subject">
        Re: {payload.subject}
    </div>
    
    <div class="body">
        {''.join(f'<p>{p}</p>' for p in payload.body.split(chr(10) + chr(10)) if p.strip())}
    </div>
    
    <div class="signature">
        Respectfully submitted,<br><br><br>
        ________________________<br>
        {{{{SENDER_NAME}}}}
    </div>
</body>
</html>
        """.strip()
