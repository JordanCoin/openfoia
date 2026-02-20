"""Campaign coordination for crowdsourced FOIA requests.

The power isn't in one request — it's in 100 people requesting
the same thing from different angles, making it impossible to
strategically delay any single journalist.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from jinja2 import Template

from .models import (
    Agency,
    Campaign,
    DeliveryMethod,
    Request,
    RequestStatus,
    User,
)


@dataclass
class CampaignTemplate:
    """Template for generating FOIA requests in a campaign."""

    name: str
    description: str
    
    # Request template (Jinja2)
    subject_template: str
    body_template: str
    
    # Variations to distribute load and avoid pattern detection
    subject_variations: list[str] = field(default_factory=list)
    intro_variations: list[str] = field(default_factory=list)
    closing_variations: list[str] = field(default_factory=list)
    
    # Parameters that requesters can customize
    customizable_fields: list[str] = field(default_factory=list)
    
    # Targeting
    target_agency_ids: list[str] = field(default_factory=list)
    recommended_method: DeliveryMethod = DeliveryMethod.EMAIL

    def render(
        self,
        participant: User,
        agency: Agency,
        custom_params: dict[str, Any] | None = None,
        randomize: bool = True,
    ) -> tuple[str, str]:
        """Render a FOIA request for a specific participant."""
        # Build context
        context = {
            'participant': participant,
            'agency': agency,
            'date': datetime.utcnow().strftime('%B %d, %Y'),
            'custom': custom_params or {},
        }
        
        # Apply variations
        if randomize and self.subject_variations:
            context['subject_variation'] = random.choice(self.subject_variations)
        if randomize and self.intro_variations:
            context['intro_variation'] = random.choice(self.intro_variations)
        if randomize and self.closing_variations:
            context['closing_variation'] = random.choice(self.closing_variations)
        
        # Render templates
        subject = Template(self.subject_template).render(**context)
        body = Template(self.body_template).render(**context)
        
        return subject, body


class CampaignCoordinator:
    """Coordinate crowdsourced FOIA campaigns.
    
    Features:
    - Distributed request generation
    - Staggered sending to avoid detection
    - Response aggregation
    - Progress tracking
    - Participant coordination
    """

    def __init__(self, db_session: Any):
        self.db = db_session

    async def create_campaign(
        self,
        name: str,
        description: str,
        template: CampaignTemplate,
        organizer: User,
        target_count: int = 100,
        ends_at: datetime | None = None,
    ) -> Campaign:
        """Create a new FOIA campaign."""
        campaign = Campaign(
            id=str(uuid4()),
            name=name,
            description=description,
            organizer_id=organizer.id,
            request_template=template.body_template,
            target_agency_ids=template.target_agency_ids,
            target_request_count=target_count,
            is_active=True,
            ends_at=ends_at,
        )
        
        self.db.add(campaign)
        await self.db.commit()
        
        return campaign

    async def join_campaign(
        self,
        campaign: Campaign,
        participant: User,
    ) -> None:
        """Add a participant to a campaign."""
        if participant not in campaign.participants:
            campaign.participants.append(participant)
            await self.db.commit()

    async def generate_request(
        self,
        campaign: Campaign,
        participant: User,
        agency: Agency,
        template: CampaignTemplate,
        custom_params: dict[str, Any] | None = None,
    ) -> Request:
        """Generate a FOIA request for a campaign participant."""
        # Render the template
        subject, body = template.render(
            participant=participant,
            agency=agency,
            custom_params=custom_params,
            randomize=True,
        )
        
        # Generate request number
        request_number = f"REQ-{datetime.utcnow().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
        
        # Determine delivery method
        if agency.foia_email and template.recommended_method == DeliveryMethod.EMAIL:
            method = DeliveryMethod.EMAIL
        elif agency.foia_fax:
            method = DeliveryMethod.FAX
        elif agency.foia_address:
            method = DeliveryMethod.MAIL
        else:
            method = DeliveryMethod.EMAIL
        
        request = Request(
            id=str(uuid4()),
            request_number=request_number,
            requester_id=participant.id,
            agency_id=agency.id,
            campaign_id=campaign.id,
            subject=subject,
            body=body,
            delivery_method=method,
            status=RequestStatus.DRAFT,
            fee_waiver_requested=True,
        )
        
        self.db.add(request)
        await self.db.commit()
        
        return request

    async def schedule_staggered_send(
        self,
        campaign: Campaign,
        requests: list[Request],
        start_time: datetime | None = None,
        spread_hours: int = 72,
    ) -> list[tuple[Request, datetime]]:
        """Schedule requests to be sent over time.
        
        Staggering avoids:
        1. Looking like a coordinated attack
        2. Overwhelming agency systems
        3. Making it easy to identify and block
        """
        start = start_time or datetime.utcnow()
        schedule = []
        
        # Distribute evenly with some randomness
        interval = timedelta(hours=spread_hours) / len(requests)
        
        for i, request in enumerate(requests):
            # Add some jitter (-30 to +30 minutes)
            jitter = timedelta(minutes=random.randint(-30, 30))
            send_time = start + (interval * i) + jitter
            
            # Avoid sending at weird hours (2-6 AM)
            while send_time.hour >= 2 and send_time.hour < 6:
                send_time += timedelta(hours=4)
            
            request.metadata = request.metadata or {}
            request.metadata['scheduled_send_time'] = send_time.isoformat()
            schedule.append((request, send_time))
        
        await self.db.commit()
        return schedule

    async def get_campaign_stats(self, campaign: Campaign) -> dict[str, Any]:
        """Get statistics for a campaign."""
        requests = campaign.requests
        
        status_counts = {}
        for request in requests:
            status = request.status.value
            status_counts[status] = status_counts.get(status, 0) + 1
        
        # Response statistics
        responded = [r for r in requests if r.status in (
            RequestStatus.PARTIAL_RESPONSE,
            RequestStatus.COMPLETE,
            RequestStatus.DENIED,
        )]
        
        avg_response_days = None
        if responded:
            response_times = [
                (r.completed_at - r.sent_at).days
                for r in responded
                if r.completed_at and r.sent_at
            ]
            if response_times:
                avg_response_days = sum(response_times) / len(response_times)
        
        # Fee statistics
        total_fees_estimated = sum(r.fee_estimate or 0 for r in requests)
        total_fees_paid = sum(r.fee_paid or 0 for r in requests)
        
        return {
            'campaign_id': campaign.id,
            'campaign_name': campaign.name,
            'is_active': campaign.is_active,
            'participant_count': len(campaign.participants),
            'request_count': len(requests),
            'target_count': campaign.target_request_count,
            'completion_percentage': len(requests) / campaign.target_request_count * 100,
            'status_breakdown': status_counts,
            'response_rate': len(responded) / len(requests) * 100 if requests else 0,
            'avg_response_days': avg_response_days,
            'total_fees_estimated': total_fees_estimated,
            'total_fees_paid': total_fees_paid,
            'denial_count': status_counts.get('denied', 0),
        }

    async def generate_progress_report(self, campaign: Campaign) -> str:
        """Generate a human-readable progress report."""
        stats = await self.get_campaign_stats(campaign)
        
        return f"""
# Campaign Progress Report: {stats['campaign_name']}

## Overview
- **Participants:** {stats['participant_count']}
- **Requests Filed:** {stats['request_count']} / {stats['target_count']} ({stats['completion_percentage']:.1f}%)
- **Response Rate:** {stats['response_rate']:.1f}%
- **Average Response Time:** {stats['avg_response_days'] or 'N/A'} days

## Status Breakdown
{chr(10).join(f'- {status}: {count}' for status, count in stats['status_breakdown'].items())}

## Financial
- **Total Fees Estimated:** ${stats['total_fees_estimated']:,.2f}
- **Total Fees Paid:** ${stats['total_fees_paid']:,.2f}

## Analysis
- **Denials:** {stats['denial_count']}
- **Active:** {'Yes' if stats['is_active'] else 'No'}

---
*Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*
        """.strip()


# Pre-built campaign templates for common use cases

CONTRACTOR_SPENDING_TEMPLATE = CampaignTemplate(
    name="Federal Contractor Spending",
    description="Request spending records for federal contractors",
    subject_template="FOIA Request: {{ agency.name }} Contracts with {{ custom.contractor_name }}",
    body_template="""
{{ intro_variation | default("I am writing to request records under the Freedom of Information Act (5 U.S.C. § 552).") }}

I request copies of the following records:

1. All contracts, task orders, and modifications between {{ agency.name }} and {{ custom.contractor_name }} from {{ custom.start_date | default("January 1, 2020") }} to present.

2. All invoices and payment records for the above contracts.

3. All performance evaluations, quality assurance reports, or contractor performance assessment reports for {{ custom.contractor_name }}.

4. All correspondence between {{ agency.name }} officials and {{ custom.contractor_name }} representatives regarding contract performance, modifications, or disputes.

{{ closing_variation | default("I am willing to pay reasonable duplication fees up to $25. Please contact me if costs will exceed this amount.") }}

If any portion of this request is denied, please cite the specific exemption(s) and explain why they apply. I also request that you release any reasonably segregable portions of otherwise exempt records.

Thank you for your prompt attention to this request.
    """.strip(),
    subject_variations=[
        "FOIA Request: Contractor Records - {{ custom.contractor_name }}",
        "Freedom of Information Request: {{ custom.contractor_name }} Contracts",
        "Records Request: {{ agency.abbreviation }} Spending on {{ custom.contractor_name }}",
    ],
    intro_variations=[
        "Pursuant to the Freedom of Information Act, I hereby request the following records:",
        "I am submitting this FOIA request for records in the possession of your agency.",
        "This is a request under FOIA (5 U.S.C. § 552) for agency records.",
    ],
    closing_variations=[
        "I am prepared to pay up to $25 in fees. Please notify me if charges will exceed this.",
        "Please contact me regarding fees before processing if they exceed $25.",
        "I request a fee waiver as this information will contribute to public understanding.",
    ],
    customizable_fields=['contractor_name', 'start_date', 'end_date'],
)


COMMUNICATIONS_TEMPLATE = CampaignTemplate(
    name="Official Communications",
    description="Request communications between officials on a topic",
    subject_template="FOIA Request: Communications Regarding {{ custom.topic }}",
    body_template="""
{{ intro_variation | default("I am writing to request records under the Freedom of Information Act.") }}

I request copies of the following records from {{ custom.start_date | default("January 1, 2023") }} to present:

1. All emails, text messages, instant messages, and other electronic communications sent or received by {{ custom.officials | default("agency leadership") }} regarding "{{ custom.topic }}".

2. All meeting notes, agendas, and minutes from meetings where "{{ custom.topic }}" was discussed.

3. All memoranda, briefing documents, and talking points regarding "{{ custom.topic }}".

4. All calendar entries related to meetings about "{{ custom.topic }}".

Search terms should include: {{ custom.search_terms | default(custom.topic) }}

{{ closing_variation | default("I request a fee waiver as disclosure is in the public interest.") }}

Please provide records in electronic format where possible.
    """.strip(),
    customizable_fields=['topic', 'officials', 'start_date', 'search_terms'],
)
