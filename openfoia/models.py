"""Domain models for OpenFOIA.

Core entities:
- Agency: Government body that handles FOIA requests
- Request: A single FOIA request with full lifecycle tracking
- Document: A received document (response, fee letter, denial, etc.)
- Entity: Extracted person, org, location, etc. from documents
- Campaign: Coordinated crowdsourced request effort
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Table,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# === Enums ===


class AgencyLevel(str, enum.Enum):
    FEDERAL = "federal"
    STATE = "state"
    LOCAL = "local"
    TRIBAL = "tribal"


class DeliveryMethod(str, enum.Enum):
    FAX = "fax"
    MAIL = "mail"
    EMAIL = "email"
    WEB_PORTAL = "web_portal"
    IN_PERSON = "in_person"


class RequestStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_SEND = "pending_send"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    PROCESSING = "processing"
    FEE_ESTIMATE = "fee_estimate"
    FEE_PAID = "fee_paid"
    PARTIAL_RESPONSE = "partial_response"
    COMPLETE = "complete"
    DENIED = "denied"
    APPEALED = "appealed"
    LITIGATION = "litigation"
    CLOSED = "closed"


class DocumentType(str, enum.Enum):
    ACKNOWLEDGMENT = "acknowledgment"
    FEE_ESTIMATE = "fee_estimate"
    PARTIAL_RESPONSE = "partial_response"
    FULL_RESPONSE = "full_response"
    DENIAL = "denial"
    GLOMAR = "glomar"  # Neither confirm nor deny
    NO_RECORDS = "no_records"
    REFERRAL = "referral"  # Referred to another agency
    APPEAL_RESPONSE = "appeal_response"
    CORRESPONDENCE = "correspondence"


class EntityType(str, enum.Enum):
    PERSON = "person"
    ORGANIZATION = "organization"
    LOCATION = "location"
    DATE = "date"
    MONEY = "money"
    DOCUMENT_ID = "document_id"
    PHONE = "phone"
    EMAIL = "email"
    ADDRESS = "address"


class ConfidenceLevel(str, enum.Enum):
    CONFIRMED = "confirmed"  # Direct evidence
    PROBABLE = "probable"    # Strong circumstantial
    POSSIBLE = "possible"    # Weak link
    UNRESOLVED = "unresolved"


# === Association Tables ===


campaign_participants = Table(
    "campaign_participants",
    Base.metadata,
    Column("campaign_id", ForeignKey("campaigns.id"), primary_key=True),
    Column("user_id", ForeignKey("users.id"), primary_key=True),
)

entity_links = Table(
    "entity_links",
    Base.metadata,
    Column("source_id", ForeignKey("entities.id"), primary_key=True),
    Column("target_id", ForeignKey("entities.id"), primary_key=True),
    Column("link_type", String(50)),
    Column("confidence", Enum(ConfidenceLevel)),
    Column("evidence", Text),
)


# === Core Models ===


class User(Base):
    """A user of the system (journalist, researcher, citizen)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    organization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_journalist: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    requests: Mapped[list["Request"]] = relationship(back_populates="requester")
    campaigns: Mapped[list["Campaign"]] = relationship(
        secondary=campaign_participants, back_populates="participants"
    )


class Agency(Base):
    """A government agency that processes FOIA requests."""

    __tablename__ = "agencies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), index=True)
    abbreviation: Mapped[str | None] = mapped_column(String(20), nullable=True)
    level: Mapped[AgencyLevel] = mapped_column(Enum(AgencyLevel))
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)  # For state/local
    
    # Contact info
    foia_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    foia_fax: Mapped[str | None] = mapped_column(String(20), nullable=True)
    foia_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    foia_portal_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    
    # Processing info
    preferred_method: Mapped[DeliveryMethod] = mapped_column(
        Enum(DeliveryMethod), default=DeliveryMethod.EMAIL
    )
    typical_response_days: Mapped[int] = mapped_column(Integer, default=20)
    fee_waiver_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # Stats (updated over time)
    avg_response_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    denial_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_requests_tracked: Mapped[int] = mapped_column(Integer, default=0)
    
    # Relationships
    requests: Mapped[list["Request"]] = relationship(back_populates="agency")


class Request(Base):
    """A single FOIA request with full lifecycle tracking."""

    __tablename__ = "requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    
    # Core fields
    requester_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    agency_id: Mapped[str] = mapped_column(ForeignKey("agencies.id"))
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    
    # Request content
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    date_range_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    date_range_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fee_waiver_requested: Mapped[bool] = mapped_column(default=True)
    expedited_requested: Mapped[bool] = mapped_column(default=False)
    
    # Delivery
    delivery_method: Mapped[DeliveryMethod] = mapped_column(Enum(DeliveryMethod))
    delivery_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Fax confirmation, tracking #
    
    # Status tracking
    status: Mapped[RequestStatus] = mapped_column(Enum(RequestStatus), default=RequestStatus.DRAFT)
    agency_tracking_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    
    # Dates
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    # Fees
    fee_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_paid: Mapped[float | None] = mapped_column(Float, nullable=True)
    
    # Metadata
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    
    # Relationships
    requester: Mapped["User"] = relationship(back_populates="requests")
    agency: Mapped["Agency"] = relationship(back_populates="requests")
    campaign: Mapped["Campaign | None"] = relationship(back_populates="requests")
    documents: Mapped[list["Document"]] = relationship(back_populates="request")
    timeline: Mapped[list["TimelineEvent"]] = relationship(back_populates="request")

    def days_pending(self) -> int:
        """Days since request was sent."""
        if not self.sent_at:
            return 0
        return (datetime.utcnow() - self.sent_at).days

    def is_overdue(self) -> bool:
        """Whether the request is past its due date."""
        if not self.due_date:
            return False
        return datetime.utcnow() > self.due_date


class Document(Base):
    """A document received in response to a FOIA request."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"))
    
    # Document info
    doc_type: Mapped[DocumentType] = mapped_column(Enum(DocumentType))
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    file_size: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(100))
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    
    # Processing status
    ocr_completed: Mapped[bool] = mapped_column(default=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    entities_extracted: Mapped[bool] = mapped_column(default=False)
    
    # Redaction tracking
    redaction_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exemptions_cited: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # e.g., ["b(6)", "b(7)(A)"]
    
    # Dates
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    # Relationships
    request: Mapped["Request"] = relationship(back_populates="documents")
    entities: Mapped[list["Entity"]] = relationship(back_populates="source_document")


class Entity(Base):
    """An entity extracted from documents (person, org, etc.)."""

    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    
    # Entity info
    entity_type: Mapped[EntityType] = mapped_column(Enum(EntityType))
    raw_text: Mapped[str] = mapped_column(Text)  # As it appeared in document
    normalized_text: Mapped[str] = mapped_column(Text, index=True)  # Cleaned/normalized
    canonical_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # Link to canonical entity
    
    # Extraction info
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)  # Surrounding text
    
    # Metadata
    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    
    # Relationships
    source_document: Mapped["Document"] = relationship(back_populates="entities")
    linked_entities: Mapped[list["Entity"]] = relationship(
        secondary=entity_links,
        primaryjoin=id == entity_links.c.source_id,
        secondaryjoin=id == entity_links.c.target_id,
    )


class Campaign(Base):
    """A coordinated crowdsourced FOIA campaign."""

    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    
    # Campaign info
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    organizer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    
    # Template
    request_template: Mapped[str] = mapped_column(Text)
    target_agency_ids: Mapped[list[str]] = mapped_column(JSON)  # List of agency IDs
    
    # Goals
    target_request_count: Mapped[int] = mapped_column(Integer, default=100)
    
    # Status
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    # Relationships
    participants: Mapped[list["User"]] = relationship(
        secondary=campaign_participants, back_populates="campaigns"
    )
    requests: Mapped[list["Request"]] = relationship(back_populates="campaign")

    def request_count(self) -> int:
        return len(self.requests)

    def completion_rate(self) -> float:
        if not self.requests:
            return 0.0
        completed = sum(1 for r in self.requests if r.status == RequestStatus.COMPLETE)
        return completed / len(self.requests)


class TimelineEvent(Base):
    """A timestamped event in a request's lifecycle."""

    __tablename__ = "timeline_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"))
    
    event_type: Mapped[str] = mapped_column(String(50))  # sent, acknowledged, response, appeal, etc.
    description: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    
    # Relationships
    request: Mapped["Request"] = relationship(back_populates="timeline")


# === Database Setup ===


def create_db(url: str = "sqlite:///openfoia.db") -> None:
    """Create all tables."""
    engine = create_engine(url)
    Base.metadata.create_all(engine)
