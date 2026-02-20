"""FOIA request templates with proven language.

These templates are based on guidance from:
- Reporters Committee for Freedom of the Press
- MuckRock's successful request patterns
- FOIA.gov sample letters

Templates are designed to be filled with specific details.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class RequesterInfo:
    """Information about the person filing the request."""
    name: str
    organization: Optional[str] = None
    address: str = ""
    email: str = ""
    phone: str = ""
    is_journalist: bool = False
    is_educational: bool = False
    publication: Optional[str] = None


@dataclass
class RequestDetails:
    """Details of what records are being requested."""
    subject: str
    description: str
    date_range_start: Optional[datetime] = None
    date_range_end: Optional[datetime] = None
    keywords: list[str] = None
    exclusions: Optional[str] = None


# === Base Request Template ===


def standard_request(
    requester: RequesterInfo,
    agency_name: str,
    details: RequestDetails,
    fee_waiver: bool = True,
    expedited: bool = False,
    max_fee: float = 25.0,
) -> str:
    """Generate a standard FOIA request letter.
    
    This is the core template that works for most federal agencies.
    Uses language proven to be effective based on RCFP guidance.
    """
    date_str = datetime.now().strftime("%B %d, %Y")
    
    # Build date range clause if provided
    date_clause = ""
    if details.date_range_start and details.date_range_end:
        start = details.date_range_start.strftime("%B %d, %Y")
        end = details.date_range_end.strftime("%B %d, %Y")
        date_clause = f" for the period from {start} through {end}"
    elif details.date_range_start:
        start = details.date_range_start.strftime("%B %d, %Y")
        date_clause = f" from {start} to the present"
    
    # Build organization line
    org_line = ""
    if requester.organization:
        org_line = f"\n{requester.organization}"
    
    letter = f"""{date_str}

{requester.name}{org_line}
{requester.address}
{requester.email}
{requester.phone}

FOIA Officer
{agency_name}

Re: Freedom of Information Act Request

Dear FOIA Officer:

Pursuant to the Freedom of Information Act, 5 U.S.C. § 552, I am requesting access to and copies of the following records{date_clause}:

{details.description}

"""
    
    # Add exclusions if specified
    if details.exclusions:
        letter += f"""This request specifically excludes:

{details.exclusions}

"""
    
    # Format preference
    letter += """I prefer to receive responsive documents in electronic format (PDF preferred) via email if possible. If electronic delivery is not available, please send paper copies to the address above.

"""
    
    # Fee section
    if fee_waiver:
        letter += generate_fee_waiver_justification(requester)
    else:
        letter += f"""I am willing to pay reasonable fees for the processing of this request up to ${max_fee:.2f}. If the fees will exceed this amount, please contact me before proceeding.

"""
    
    # Expedited processing
    if expedited:
        letter += generate_expedited_justification(requester, details)
    
    # Standard closing
    letter += """If you determine that any portion of the records I have requested is exempt from disclosure, please provide me with an index of those records and the specific exemption(s) that you believe apply to each withheld document or portion thereof, as required by Vaughn v. Rosen.

If my request is denied in whole or in part, I ask that you justify all deletions by reference to specific exemptions of the Act. I expect the release of all segregable portions of otherwise exempt material. I reserve the right to appeal any decision to withhold information or deny a waiver of fees.

I look forward to your response within 20 business days, as required by statute.

Sincerely,

{requester.name}
"""
    
    return letter


def generate_fee_waiver_justification(requester: RequesterInfo) -> str:
    """Generate fee waiver justification based on requester status.
    
    Fee waivers are granted when disclosure is:
    1. In the public interest because it is likely to contribute significantly 
       to public understanding of government operations or activities, AND
    2. Not primarily in the commercial interest of the requester.
    """
    
    justification = """**Fee Waiver Request**

I request a waiver of all fees associated with this request pursuant to 5 U.S.C. § 552(a)(4)(A)(iii). Disclosure of the requested information is in the public interest because:

"""
    
    if requester.is_journalist:
        publication = requester.publication or "various news outlets"
        justification += f"""1. **I am a representative of the news media.** I am a journalist who writes for {publication}. The records I seek will be used to gather information for potential publication that will inform the public about government operations and activities.

2. **Disclosure will contribute significantly to public understanding.** The information I have requested is not currently publicly available. My analysis and reporting will provide meaningful public benefit by shedding light on government activities.

3. **Disclosure is not primarily in my commercial interest.** My purpose in requesting these records is to inform the public, not for commercial gain. Any publication will be freely accessible to the public.

As a representative of the news media, I am entitled to reduced fees under FOIA. I am only required to pay for duplication costs, and those costs should be waived entirely because disclosure is in the public interest.

"""
    elif requester.is_educational:
        org = requester.organization or "an educational institution"
        justification += f"""1. **I am affiliated with an educational institution.** I am making this request on behalf of {org} for scholarly or academic purposes.

2. **Disclosure will contribute to public understanding.** The records I seek will be used for research and educational purposes that will contribute to the body of public knowledge about government operations.

3. **Disclosure is not primarily in my commercial interest.** This request is made for educational purposes, not for commercial gain.

As an educational requester, I am entitled to reduced fees under FOIA.

"""
    else:
        justification += """1. **Disclosure will contribute significantly to public understanding of government operations.** The records I seek concern operations or activities of the government that are not currently publicly known. This information will shed light on the federal government's performance of its statutory duties.

2. **The information is meaningful and will reach a broad audience.** I intend to share my findings publicly, contributing to greater public understanding of government activities.

3. **Disclosure is not primarily in my commercial interest.** I am a private citizen with no commercial interest in this information. My sole interest is in understanding how my government operates.

"""
    
    justification += """If you deny my fee waiver request, please provide a detailed explanation of the basis for your denial. If fees will exceed $25, please contact me before processing the request.

"""
    
    return justification


def generate_expedited_justification(
    requester: RequesterInfo,
    details: RequestDetails,
) -> str:
    """Generate expedited processing justification.
    
    Expedited processing is granted when there is:
    1. An imminent threat to life or physical safety
    2. An urgency to inform the public about government activity (for news media)
    3. Loss of substantial due process rights
    4. A matter of widespread and exceptional media interest with possible federal government involvement
    """
    
    justification = """**Request for Expedited Processing**

I request expedited processing of this FOIA request pursuant to 5 U.S.C. § 552(a)(6)(E).

"""
    
    if requester.is_journalist:
        publication = requester.publication or "news media outlets"
        justification += f"""I am a journalist working under a deadline to inform the public about an actual or alleged federal government activity. The records I seek relate to a matter of current public interest. I am working on an article for {publication} regarding {details.subject}.

There is an urgent need to inform the public about government activity because the information I seek concerns matters of significant public concern that are currently in the news.

I certify that the above statements are true and correct to the best of my knowledge.

"""
    else:
        justification += f"""The records I seek involve a matter of exceptional media interest and urgency. {details.subject} is currently receiving widespread public attention, and the requested records would significantly contribute to the public's understanding of the government's role in this matter.

Delay in obtaining these records would harm the public interest by preventing timely public scrutiny of government activities.

I certify that the above statements are true and correct to the best of my knowledge.

"""
    
    return justification


# === Appeal Templates ===


def appeal_denial(
    requester: RequesterInfo,
    agency_name: str,
    original_request_date: datetime,
    denial_date: datetime,
    tracking_number: str,
    exemptions_cited: list[str],
    appeal_reasons: str,
) -> str:
    """Generate an appeal letter for a FOIA denial.
    
    Appeals must generally be filed within 90 days of the denial.
    """
    date_str = datetime.now().strftime("%B %d, %Y")
    request_date = original_request_date.strftime("%B %d, %Y")
    denial_date_str = denial_date.strftime("%B %d, %Y")
    
    exemptions_str = ", ".join(exemptions_cited) if exemptions_cited else "unspecified exemptions"
    
    letter = f"""{date_str}

{requester.name}
{requester.address}
{requester.email}

FOIA Appeals Officer
{agency_name}

Re: Freedom of Information Act Appeal
Original Request Date: {request_date}
Tracking Number: {tracking_number}
Denial Date: {denial_date_str}

Dear FOIA Appeals Officer:

I am writing to appeal the denial of my Freedom of Information Act request dated {request_date}, which was denied on {denial_date_str} (tracking number: {tracking_number}).

The agency cited {exemptions_str} as the basis for withholding records. I respectfully appeal this determination for the following reasons:

{appeal_reasons}

**Request for Segregable Portions**

If the agency maintains that certain portions of the requested records are exempt from disclosure, I request that all reasonably segregable, non-exempt portions be released, as required by 5 U.S.C. § 552(b).

**Request for Vaughn Index**

If records continue to be withheld on appeal, I request a Vaughn index that:
1. Identifies each document or portion thereof being withheld
2. States the exemption(s) claimed for each withholding
3. Explains how disclosure would harm the interest protected by the exemption

I look forward to your response within 20 business days, as required by statute.

Sincerely,

{requester.name}
"""
    
    return letter


# === Exemption-Specific Appeal Language ===


EXEMPTION_APPEAL_ARGUMENTS = {
    "b(1)": """**Regarding Exemption (b)(1) - National Security:**
The agency has not demonstrated that the withheld information is currently and properly classified under Executive Order 13526 or a predecessor order. I request that the agency conduct a discretionary review to determine whether continued classification is warranted. Additionally, the passage of time may have reduced or eliminated any national security sensitivity of the records.""",
    
    "b(2)": """**Regarding Exemption (b)(2) - Internal Personnel Rules:**
Under Milner v. Department of Navy (2011), Exemption 2 is limited to records relating to employee relations and human resources matters. The requested records do not fall within this narrow scope.""",
    
    "b(3)": """**Regarding Exemption (b)(3) - Statutory Exemption:**
The agency has not identified the specific statute that prohibits disclosure or explained how it applies to the withheld records. If a statute is cited, I request the agency demonstrate that the statute either: (A) requires withholding in a manner that leaves no discretion, or (B) establishes particular criteria for withholding.""",
    
    "b(4)": """**Regarding Exemption (b)(4) - Trade Secrets/Commercial Information:**
The agency has not demonstrated that the withheld information constitutes trade secrets or that disclosure would cause substantial competitive harm to the submitter. Information provided to the government in the performance of a contract may lose its confidential character.""",
    
    "b(5)": """**Regarding Exemption (b)(5) - Deliberative Process:**
This exemption protects only pre-decisional, deliberative communications. It does not protect: (1) factual information, (2) final agency decisions, (3) statements of policy, or (4) documents adopted by an agency as its official position. The agency has not demonstrated that the withheld material is both pre-decisional and deliberative.""",
    
    "b(6)": """**Regarding Exemption (b)(6) - Personal Privacy:**
The public interest in disclosure outweighs any privacy interest. The individuals involved are government officials acting in their official capacity, and the public has a right to know how government officials perform their duties. Names and identifying information about government employees performing official duties should be released.""",
    
    "b(7)(A)": """**Regarding Exemption (b)(7)(A) - Law Enforcement/Interference:**
The agency has not demonstrated that disclosure would interfere with ongoing enforcement proceedings. If an investigation has concluded, this exemption no longer applies. I request that the agency identify any pending enforcement actions and release all records not related to active proceedings.""",
    
    "b(7)(C)": """**Regarding Exemption (b)(7)(C) - Law Enforcement/Personal Privacy:**
The public interest in understanding government activities outweighs privacy interests in this context. Information about government officials performing their official duties should be released. The agency has not articulated specific, identified harms from disclosure.""",
    
    "b(7)(E)": """**Regarding Exemption (b)(7)(E) - Law Enforcement Techniques:**
The agency has not demonstrated that disclosure would risk circumvention of the law. Techniques or procedures that are already publicly known, obsolete, or no longer in use should be released.""",
}


def get_exemption_appeal_language(exemptions: list[str]) -> str:
    """Get appeal language for specific exemptions cited."""
    arguments = []
    for exemption in exemptions:
        # Normalize exemption format
        normalized = exemption.lower().replace(" ", "").replace("(", "(").replace(")", ")")
        if not normalized.startswith("b"):
            normalized = f"b({normalized})"
        
        # Find matching argument
        for key, argument in EXEMPTION_APPEAL_ARGUMENTS.items():
            if key.lower() == normalized or normalized.startswith(key.lower()):
                arguments.append(argument)
                break
    
    return "\n\n".join(arguments) if arguments else ""


# === Specialized Templates ===


def records_about_self(
    requester: RequesterInfo,
    agency_name: str,
    record_type: str = "all records",
    include_privacy_act: bool = True,
) -> str:
    """Generate a request for records about yourself.
    
    This template combines FOIA and Privacy Act requests for maximum coverage.
    """
    date_str = datetime.now().strftime("%B %d, %Y")
    
    letter = f"""{date_str}

{requester.name}
{requester.address}
{requester.email}

FOIA/Privacy Act Officer
{agency_name}

Re: Freedom of Information Act and Privacy Act Request for Records About Myself

Dear FOIA/Privacy Act Officer:

"""
    
    if include_privacy_act:
        letter += """Pursuant to the Freedom of Information Act, 5 U.S.C. § 552, and the Privacy Act, 5 U.S.C. § 552a, I am requesting access to and copies of any and all records maintained by your agency pertaining to me.

"""
    else:
        letter += """Pursuant to the Freedom of Information Act, 5 U.S.C. § 552, I am requesting access to and copies of any and all records maintained by your agency pertaining to me.

"""
    
    letter += f"""Specifically, I am requesting {record_type}.

**Verification of Identity**

For purposes of verifying my identity, I provide the following information:

Full Name: {requester.name}
Address: {requester.address}
Email: {requester.email}

I declare under penalty of perjury that I am the person named above and that the information I have provided is true and correct.

"""
    
    if include_privacy_act:
        letter += """**Privacy Act Request**

Under the Privacy Act, I am entitled to access records maintained about me in systems of records. I request that you search all relevant systems of records and provide me with copies of any records found.

"""
    
    letter += """I prefer to receive responsive documents in electronic format (PDF preferred) via email if possible.

I request a fee waiver for this request. As I am seeking records about myself for personal use, disclosure is in my interest and not for commercial purposes.

I look forward to your response within 20 business days.

Sincerely,

{requester.name}
"""
    
    return letter


# === CLI Integration ===


def list_templates() -> list[dict]:
    """Return list of available templates for CLI display."""
    return [
        {
            "name": "standard",
            "description": "Standard FOIA request for any agency",
            "function": "standard_request",
        },
        {
            "name": "appeal",
            "description": "Appeal a FOIA denial",
            "function": "appeal_denial",
        },
        {
            "name": "self",
            "description": "Request records about yourself (FOIA + Privacy Act)",
            "function": "records_about_self",
        },
    ]
