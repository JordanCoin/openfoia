"""Base gateway interface for FOIA request delivery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DeliveryResult:
    """Result of a delivery attempt."""

    status: DeliveryStatus
    reference_id: str  # Provider's tracking ID
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    error_message: str | None = None
    cost_cents: int | None = None
    metadata: dict[str, Any] | None = None

    @property
    def success(self) -> bool:
        return self.status in (DeliveryStatus.SENT, DeliveryStatus.DELIVERED)


@dataclass
class DeliveryPayload:
    """Content to be delivered."""

    recipient_name: str
    recipient_address: str  # Fax number, email, mailing address, etc.
    subject: str
    body: str
    attachments: list[tuple[str, bytes]] | None = None  # (filename, content)
    cover_page: bool = True
    return_address: str | None = None


class DeliveryGateway(ABC):
    """Abstract base for delivery gateways."""

    @abstractmethod
    async def send(self, payload: DeliveryPayload) -> DeliveryResult:
        """Send a FOIA request via this gateway."""
        ...

    @abstractmethod
    async def check_status(self, reference_id: str) -> DeliveryResult:
        """Check the status of a previously sent request."""
        ...

    @abstractmethod
    async def cancel(self, reference_id: str) -> bool:
        """Cancel a pending delivery if possible."""
        ...

    @abstractmethod
    def estimate_cost(self, payload: DeliveryPayload) -> int:
        """Estimate cost in cents for this delivery."""
        ...
