"""Delivery gateways for sending FOIA requests.

Adapters for different delivery methods:
- Fax (via Twilio)
- Mail (via Lob)
- Email (via SMTP or API)
- Web Portal (via browser automation)
"""

from .base import DeliveryGateway, DeliveryResult
from .fax import TwilioFaxGateway
from .mail import LobMailGateway
from .email import EmailGateway

__all__ = [
    "DeliveryGateway",
    "DeliveryResult",
    "TwilioFaxGateway",
    "LobMailGateway",
    "EmailGateway",
]
