"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-21

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Users
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("organization", sa.String(255), nullable=True),
        sa.Column("is_journalist", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # Agencies
    op.create_table(
        "agencies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("abbreviation", sa.String(20), nullable=True),
        sa.Column(
            "level",
            sa.Enum("federal", "state", "local", "tribal", name="agencylevel"),
            nullable=False,
        ),
        sa.Column("state", sa.String(2), nullable=True),
        sa.Column("foia_email", sa.String(255), nullable=True),
        sa.Column("foia_fax", sa.String(20), nullable=True),
        sa.Column("foia_address", sa.Text(), nullable=True),
        sa.Column("foia_portal_url", sa.String(500), nullable=True),
        sa.Column(
            "preferred_method",
            sa.Enum("fax", "mail", "email", "web_portal", "in_person", name="deliverymethod"),
            nullable=False,
        ),
        sa.Column("typical_response_days", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("fee_waiver_criteria", sa.Text(), nullable=True),
        sa.Column("avg_response_days", sa.Float(), nullable=True),
        sa.Column("denial_rate", sa.Float(), nullable=True),
        sa.Column("total_requests_tracked", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_agencies_name", "agencies", ["name"])

    # Campaigns
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("organizer_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_template", sa.Text(), nullable=False),
        sa.Column("target_agency_ids", sa.JSON(), nullable=False),
        sa.Column("target_request_count", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
    )

    # Requests
    op.create_table(
        "requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_number", sa.String(50), nullable=False, unique=True),
        sa.Column("requester_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agency_id", sa.String(36), sa.ForeignKey("agencies.id"), nullable=False),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id"), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("date_range_start", sa.DateTime(), nullable=True),
        sa.Column("date_range_end", sa.DateTime(), nullable=True),
        sa.Column("fee_waiver_requested", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("expedited_requested", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column(
            "delivery_method",
            sa.Enum("fax", "mail", "email", "web_portal", "in_person", name="deliverymethod"),
            nullable=False,
        ),
        sa.Column("delivery_reference", sa.String(255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "pending_send",
                "sent",
                "acknowledged",
                "processing",
                "fee_estimate",
                "fee_paid",
                "partial_response",
                "complete",
                "denied",
                "appealed",
                "litigation",
                "closed",
                name="requeststatus",
            ),
            nullable=False,
        ),
        sa.Column("agency_tracking_number", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("due_date", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("fee_estimate", sa.Float(), nullable=True),
        sa.Column("fee_paid", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
    )
    op.create_index("ix_requests_request_number", "requests", ["request_number"])

    # Documents
    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column(
            "doc_type",
            sa.Enum(
                "acknowledgment",
                "fee_estimate",
                "partial_response",
                "full_response",
                "denial",
                "glomar",
                "no_records",
                "referral",
                "appeal_response",
                "correspondence",
                name="documenttype",
            ),
            nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("ocr_completed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("entities_extracted", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("redaction_count", sa.Integer(), nullable=True),
        sa.Column("exemptions_cited", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
    )

    # Entities
    op.create_table(
        "entities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column(
            "entity_type",
            sa.Enum(
                "person",
                "organization",
                "location",
                "date",
                "money",
                "document_id",
                "phone",
                "email",
                "address",
                name="entitytype",
            ),
            nullable=False,
        ),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("canonical_id", sa.String(36), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
    )
    op.create_index("ix_entities_normalized_text", "entities", ["normalized_text"])

    # Timeline Events
    op.create_table(
        "timeline_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("requests.id"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
    )

    # Association: campaign_participants
    op.create_table(
        "campaign_participants",
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id"), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
    )

    # Association: entity_links
    op.create_table(
        "entity_links",
        sa.Column("source_id", sa.String(36), sa.ForeignKey("entities.id"), primary_key=True),
        sa.Column("target_id", sa.String(36), sa.ForeignKey("entities.id"), primary_key=True),
        sa.Column("link_type", sa.String(50), nullable=True),
        sa.Column(
            "confidence",
            sa.Enum("confirmed", "probable", "possible", "unresolved", name="confidencelevel"),
            nullable=True,
        ),
        sa.Column("evidence", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("entity_links")
    op.drop_table("campaign_participants")
    op.drop_table("timeline_events")
    op.drop_table("entities")
    op.drop_table("documents")
    op.drop_table("requests")
    op.drop_table("campaigns")
    op.drop_table("agencies")
    op.drop_table("users")
