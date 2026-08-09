"""Physical mail gateway using Lob API.

Sends FOIA requests as certified physical letters via the Lob print-and-mail
API. Letters are printed, stuffed, stamped, and mailed by Lob with USPS
tracking included.

Lob API v6+ uses lob-python with Configuration/ApiClient pattern.
See: https://docs.lob.com/
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime
from typing import Any

from .base import DeliveryGateway, DeliveryPayload, DeliveryResult, DeliveryStatus


def _esc(value: Any) -> str:
    """HTML-escape a field interpolated into the printed letter.

    The body was already escaped, but recipient/sender names and the subject
    were not — and those can come from fetched agency records, so markup in
    them would distort or inject content into the rendered letter.
    """
    return html.escape(str(value or ""), quote=True)


logger = logging.getLogger(__name__)


class LobMailGateway(DeliveryGateway):
    """Send FOIA requests via physical mail using Lob.

    Lob API:
    - Letters start at ~$0.63 (First Class, single page)
    - Certified mail available (+$4.00 approx)
    - USPS tracking included for all letters
    - 3-5 business days delivery (First Class)
    - 2-3 business days delivery (Express)

    Usage::

        gateway = LobMailGateway(
            api_key="test_xxxx",
            return_address={
                "name": "Jane Doe",
                "address_line1": "123 Main St",
                "address_city": "Washington",
                "address_state": "DC",
                "address_zip": "20001",
            },
        )
        result = await gateway.send(payload)
    """

    # Approximate costs in cents
    BASE_COST_CENTS = 63  # First-class single page
    EXTRA_PAGE_COST_CENTS = 15  # Per additional page
    CERTIFIED_COST_CENTS = 400  # Additional for certified
    RETURN_ENVELOPE_CENTS = 50  # Return envelope

    def __init__(
        self,
        api_key: str,
        return_address: dict[str, str],
        use_certified: bool = True,
        address_verification: bool = True,
    ):
        self.api_key = api_key
        self.return_address = return_address
        self.use_certified = use_certified
        self.address_verification = address_verification
        self._letters_api: Any = None
        self._api_client: Any = None

    def _get_lob(self) -> Any:
        """Lazily initialize Lob client."""
        if self._letters_api is None:
            import lob

            lob.api_key = self.api_key
            self._letters_api = lob
        return self._letters_api

    async def send(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send FOIA request as a physical letter via Lob.

        Formats the request as a professional letter with proper addressing,
        sends it via Lob's print-and-mail API, and returns tracking info.
        """
        try:
            # Availability probe: raises ImportError if the extra is missing.
            import lob  # noqa: F401

            lob_client = self._get_lob()

            # Parse recipient address into Lob format
            to_address = self._parse_address(payload.recipient_address)

            # Generate letter HTML with proper formatting
            letter_html = self._generate_letter_html(payload)

            pages = self._estimate_pages(payload)
            logger.info(
                "Sending letter to %s (%d estimated pages, certified=%s)",
                payload.recipient_name,
                pages,
                self.use_certified,
            )

            # Send via Lob API (v4)
            letter = await asyncio.to_thread(
                lob_client.Letter.create,
                description=f"FOIA Request: {payload.subject[:50]}",
                to_address=to_address,
                from_address=self.return_address,
                file=letter_html,
                color=False,
                mail_type="usps_first_class",
                extra_service="certified" if self.use_certified else None,
                return_envelope=True,
            )

            # Extract tracking info
            tracking_number = None
            expected_delivery = None
            if hasattr(letter, "tracking_number"):
                tracking_number = letter.tracking_number
            if hasattr(letter, "expected_delivery_date"):
                expected_delivery = str(letter.expected_delivery_date)

            logger.info(
                "Letter created: id=%s tracking=%s",
                letter.id,
                tracking_number,
            )

            return DeliveryResult(
                status=DeliveryStatus.SENT,
                reference_id=letter.id,
                sent_at=datetime.now(UTC),
                cost_cents=self.estimate_cost(payload),
                metadata={
                    "tracking_number": tracking_number,
                    "expected_delivery_date": expected_delivery,
                    "carrier": "USPS",
                    "mail_type": "certified" if self.use_certified else "first_class",
                    "pages": pages,
                    "return_envelope": True,
                },
            )

        except ImportError:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message="Mail gateway not installed. Run: openfoia install-extras mail",
            )
        except Exception as e:
            logger.error("Letter send failed: %s", e)
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id="",
                error_message=str(e),
            )

    async def check_status(self, reference_id: str) -> DeliveryResult:
        """Check letter delivery status via Lob tracking.

        Lob tracking events include: Mailed, In Transit, In Local Area,
        Processed for Delivery, Delivered, Re-Routed, Returned to Sender.
        """
        try:
            lob_client = self._get_lob()
            letter = await asyncio.to_thread(
                lob_client.Letter.retrieve,
                reference_id,
            )

            # Determine delivery status from tracking events
            status = DeliveryStatus.SENT
            delivered_at = None
            tracking_events = []

            if hasattr(letter, "tracking_events") and letter.tracking_events:
                for event in letter.tracking_events:
                    tracking_events.append(
                        {
                            "type": getattr(event, "type", None),
                            "name": getattr(event, "name", None),
                            "time": str(getattr(event, "time", "")),
                            "location": getattr(event, "location", None),
                        }
                    )

                latest = letter.tracking_events[-1]
                event_name = getattr(latest, "name", "") or ""
                event_type = getattr(latest, "type", "") or ""

                if "delivered" in event_name.lower() or "delivered" in event_type.lower():
                    status = DeliveryStatus.DELIVERED
                    delivered_at = getattr(latest, "time", None)
                elif "returned" in event_name.lower() or "re-routed" in event_name.lower():
                    status = DeliveryStatus.FAILED

            return DeliveryResult(
                status=status,
                reference_id=reference_id,
                sent_at=getattr(letter, "send_date", None),
                delivered_at=delivered_at,
                cost_cents=None,
                metadata={
                    "tracking_number": getattr(letter, "tracking_number", None),
                    "tracking_events": tracking_events,
                    "carrier": "USPS",
                },
            )

        except Exception as e:
            logger.error("Letter status check failed for %s: %s", reference_id, e)
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                reference_id=reference_id,
                error_message=str(e),
            )

    async def cancel(self, reference_id: str) -> bool:
        """Cancel a letter (only possible before it has been printed).

        Once Lob has sent the letter to the printer, cancellation is no
        longer possible. Typically there is a ~1 hour window.
        """
        try:
            lob_client = self._get_lob()
            await asyncio.to_thread(
                lob_client.Letter.delete,
                reference_id,
            )
            logger.info("Letter %s canceled", reference_id)
            return True
        except Exception as e:
            logger.warning("Failed to cancel letter %s: %s", reference_id, e)
            return False

    def estimate_cost(self, payload: DeliveryPayload) -> int:
        """Estimate mailing cost in cents.

        Approximate costs:
        - First class letter: $0.63-$1.20 depending on page count
        - Certified mail surcharge: ~$4.00
        - Return envelope: ~$0.50
        """
        pages = self._estimate_pages(payload)

        # Base + extra pages
        cost = self.BASE_COST_CENTS + max(0, (pages - 1) * self.EXTRA_PAGE_COST_CENTS)

        # Certified mail
        if self.use_certified:
            cost += self.CERTIFIED_COST_CENTS

        # Return envelope
        cost += self.RETURN_ENVELOPE_CENTS

        return cost

    def _estimate_pages(self, payload: DeliveryPayload) -> int:
        """Estimate number of printed pages."""
        # ~3000 characters per page at 12pt with margins
        pages = max(1, len(payload.body) // 3000 + 1)

        if payload.attachments:
            for _filename, content in payload.attachments:
                pages += max(1, len(content) // 3000 + 1)

        return pages

    def _parse_address(self, address_str: str) -> dict[str, str]:
        """Parse a multi-line address string into Lob address dict.

        Expected formats::

            Name
            123 Main Street
            Washington, DC 20001

        Or with a second address line::

            Name
            Department of Records
            123 Main Street
            Washington, DC 20001
        """
        lines = [line.strip() for line in address_str.strip().split("\n") if line.strip()]

        if len(lines) < 3:
            raise ValueError(
                f"Address must have at least 3 lines (name, street, city/state/zip). "
                f"Got {len(lines)} lines: {address_str!r}"
            )

        name = lines[0]
        city_state_zip = lines[-1]

        # Parse "City, State ZIP" or "City, ST ZIP-XXXX"
        match = re.match(
            r"^(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$",
            city_state_zip,
        )
        if not match:
            raise ValueError(
                f"Could not parse city/state/zip from: {city_state_zip!r}. "
                f"Expected format: 'City, ST 12345' or 'City, ST 12345-6789'"
            )

        city, state, zip_code = match.groups()

        # Street address is everything between name and city line
        address_lines = lines[1:-1]
        address_line1 = address_lines[0]
        address_line2 = address_lines[1] if len(address_lines) > 1 else None

        result: dict[str, str] = {
            "name": name,
            "address_line1": address_line1,
            "address_city": city,
            "address_state": state,
            "address_zip": zip_code,
            "address_country": "US",
        }

        if address_line2:
            result["address_line2"] = address_line2

        return result

    def _generate_letter_html(self, payload: DeliveryPayload) -> str:
        """Generate a properly formatted letter as HTML for Lob printing.

        Follows standard business letter format with:
        - Sender address (top right)
        - Date
        - Recipient address block
        - Subject line
        - FOIA request body
        - Fee waiver language
        - Signature block

        Lob renders HTML to PDF for printing. The template uses standard
        fonts and margins suitable for USPS mailing.
        """
        date_str = datetime.now(UTC).strftime("%B %d, %Y")
        sender_name = self.return_address.get("name", "[Requester Name]")

        # Build sender address block for letterhead
        sender_parts = [sender_name]
        if addr1 := self.return_address.get("address_line1"):
            sender_parts.append(addr1)
        if addr2 := self.return_address.get("address_line2"):
            sender_parts.append(addr2)
        city = self.return_address.get("address_city", "")
        state = self.return_address.get("address_state", "")
        zip_code = self.return_address.get("address_zip", "")
        if city and state:
            sender_parts.append(f"{city}, {state} {zip_code}")

        sender_block = "<br>\n        ".join(sender_parts)

        # Format body paragraphs as HTML
        body_paragraphs = ""
        for para in payload.body.split("\n\n"):
            text = para.strip()
            if text:
                # Escape HTML entities
                text = (
                    text.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace("\n", "<br>")
                )
                body_paragraphs += f"        <p>{text}</p>\n"

        # Format recipient address for the letter
        recipient_addr = payload.recipient_address.replace("\n", "<br>")

        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        @page {{
            size: 8.5in 11in;
            margin: 1in;
        }}
        body {{
            font-family: 'Times New Roman', 'Georgia', serif;
            font-size: 12pt;
            line-height: 1.5;
            color: #000000;
        }}
        .sender {{
            text-align: right;
            margin-bottom: 0.25in;
        }}
        .date {{
            margin-bottom: 0.25in;
        }}
        .recipient {{
            margin-bottom: 0.35in;
        }}
        .subject {{
            font-weight: bold;
            margin-bottom: 0.25in;
        }}
        .salutation {{
            margin-bottom: 0.15in;
        }}
        .body p {{
            text-align: justify;
            margin-bottom: 0.12in;
        }}
        .closing {{
            margin-top: 0.35in;
        }}
        .signature {{
            margin-top: 0.5in;
        }}
        .legal-notice {{
            font-size: 10pt;
            font-style: italic;
            margin-top: 0.25in;
            border-top: 1px solid #cccccc;
            padding-top: 0.15in;
        }}
    </style>
</head>
<body>
    <div class="sender">
        {sender_block}
    </div>

    <div class="date">
        {date_str}
    </div>

    <div class="recipient">
        {_esc(payload.recipient_name)}<br>
        {recipient_addr}
    </div>

    <div class="subject">
        Re: Freedom of Information Act Request &mdash; {_esc(payload.subject)}
    </div>

    <div class="salutation">
        Dear FOIA Officer,
    </div>

    <div class="body">
        <p>This is a request under the Freedom of Information Act (FOIA), 5 U.S.C. &sect; 552.</p>

{body_paragraphs}
        <p>I request a fee waiver for this request. Disclosure of the requested information
        is in the public interest because it is likely to contribute significantly to public
        understanding of the operations or activities of the government and is not primarily
        in my commercial interest.</p>

        <p>If my request is denied in whole or in part, I ask that you justify all deletions
        by reference to specific exemptions of the Act. I expect a response within 20 business
        days as the statute requires.</p>

        <p>Thank you for your prompt attention to this matter.</p>
    </div>

    <div class="closing">
        Respectfully submitted,
    </div>

    <div class="signature">
        {_esc(sender_name)}
    </div>

    <div class="legal-notice">
        Sent via certified mail with return receipt requested.
    </div>
</body>
</html>"""
