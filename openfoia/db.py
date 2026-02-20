"""Database session management for OpenFOIA.

All data stored locally in ~/.openfoia/data.db
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Agency, AgencyLevel, DeliveryMethod


def get_data_dir() -> Path:
    """Get the OpenFOIA data directory, creating if needed."""
    data_dir = Path.home() / ".openfoia"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_db_path() -> Path:
    """Get the database file path."""
    return get_data_dir() / "data.db"


def get_engine(db_path: Path | None = None) -> Engine:
    """Create a SQLAlchemy engine."""
    if db_path is None:
        db_path = get_db_path()
    
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, echo=False)
    
    # Enable foreign keys for SQLite
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    
    return engine


def get_session_factory(engine: Engine | None = None) -> sessionmaker:
    """Create a session factory."""
    if engine is None:
        engine = get_engine()
    return sessionmaker(bind=engine)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Get a database session with automatic commit/rollback."""
    engine = get_engine()
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(seed: bool = True) -> None:
    """Initialize the database, creating tables and optionally seeding data."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    
    if seed:
        seed_agencies(engine)


def seed_agencies(engine: Engine) -> int:
    """Seed the database with federal agencies. Returns count of agencies added."""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    
    # Check if agencies already exist
    existing = session.query(Agency).count()
    if existing > 0:
        session.close()
        return 0
    
    agencies_data = get_federal_agencies()
    
    for data in agencies_data:
        agency = Agency(
            name=data["name"],
            abbreviation=data.get("abbreviation"),
            level=AgencyLevel.FEDERAL,
            foia_email=data.get("foia_email"),
            foia_fax=data.get("foia_fax"),
            foia_address=data.get("foia_address"),
            foia_portal_url=data.get("foia_portal_url"),
            preferred_method=DeliveryMethod(data.get("preferred_method", "email")),
            typical_response_days=data.get("typical_response_days", 20),
            fee_waiver_criteria=data.get("fee_waiver_criteria"),
        )
        session.add(agency)
    
    session.commit()
    count = len(agencies_data)
    session.close()
    return count


def get_federal_agencies() -> list[dict]:
    """Return federal agency seed data.
    
    Sources:
    - https://www.foia.gov/agency-search.html
    - Individual agency FOIA pages
    """
    return [
        # Intelligence & Security
        {
            "name": "Central Intelligence Agency",
            "abbreviation": "CIA",
            "foia_email": "foiacoordinator@ucia.gov",
            "foia_fax": "+1-703-613-3007",
            "foia_address": "Central Intelligence Agency\nInformation and Privacy Coordinator\nWashington, DC 20505",
            "foia_portal_url": "https://www.cia.gov/readingroom/",
            "preferred_method": "mail",
            "typical_response_days": 45,
            "fee_waiver_criteria": "Requester must demonstrate they are a representative of the news media or the information will contribute significantly to public understanding.",
        },
        {
            "name": "Federal Bureau of Investigation",
            "abbreviation": "FBI",
            "foia_email": "foiparequest@fbi.gov",
            "foia_fax": "+1-540-868-4391",
            "foia_address": "Federal Bureau of Investigation\nRecord/Information Dissemination Section\n170 Marcel Drive\nWinchester, VA 22602-4843",
            "foia_portal_url": "https://vault.fbi.gov/",
            "preferred_method": "email",
            "typical_response_days": 60,
            "fee_waiver_criteria": "News media representatives and educational/scientific institutions may qualify for reduced fees.",
        },
        {
            "name": "National Security Agency",
            "abbreviation": "NSA",
            "foia_email": "nsafoia@nsa.gov",
            "foia_address": "National Security Agency\nFOIA/PA Office (DJ4)\n9800 Savage Road, Suite 6932\nFt. George G. Meade, MD 20755-6932",
            "foia_portal_url": "https://www.nsa.gov/resources/everyone/foia/",
            "preferred_method": "email",
            "typical_response_days": 90,
        },
        {
            "name": "Department of Homeland Security",
            "abbreviation": "DHS",
            "foia_email": "foia@hq.dhs.gov",
            "foia_address": "Department of Homeland Security\nPrivacy Office, Mail Stop 0655\n2707 Martin Luther King Jr Ave SE\nWashington, DC 20528-0655",
            "foia_portal_url": "https://www.dhs.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        
        # Justice
        {
            "name": "Department of Justice",
            "abbreviation": "DOJ",
            "foia_email": "MRUFOIA.Requests@usdoj.gov",
            "foia_address": "Director, Office of Information Policy\nU.S. Department of Justice\n441 G Street, NW, 6th Floor\nWashington, DC 20530",
            "foia_portal_url": "https://www.justice.gov/oip/submit-and-track-request-or-appeal",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Bureau of Alcohol, Tobacco, Firearms and Explosives",
            "abbreviation": "ATF",
            "foia_email": "FOIAMail@atf.gov",
            "foia_address": "ATF FOIA Office\n244 Needy Road, Suite 1600\nMartinsburg, WV 25405",
            "foia_portal_url": "https://www.atf.gov/resource-center/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Drug Enforcement Administration",
            "abbreviation": "DEA",
            "foia_email": "DEA.FOIA@usdoj.gov",
            "foia_address": "Drug Enforcement Administration\nFOIA/PA Section\n8701 Morrissette Drive\nSpringfield, VA 22152",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "U.S. Marshals Service",
            "abbreviation": "USMS",
            "foia_email": "usms.foia@usdoj.gov",
            "foia_address": "U.S. Marshals Service\nFOIA/PA Officer\nCS-3, 10th Floor\nWashington, DC 20530",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        
        # Defense
        {
            "name": "Department of Defense",
            "abbreviation": "DOD",
            "foia_email": "osd.foia@mail.mil",
            "foia_address": "Office of the Secretary of Defense/Joint Staff FOIA Requester Service Center\n1155 Defense Pentagon\nWashington, DC 20301-1155",
            "foia_portal_url": "https://www.esd.whs.mil/FOIA/",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "Department of the Army",
            "abbreviation": "Army",
            "foia_email": "usarmy.pentagon.hqda-oaa.mbx.rmda-foia@mail.mil",
            "foia_address": "Department of the Army\nFOIA/PA Office\nRoom 1146, Pentagon\nWashington, DC 20310",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "Department of the Navy",
            "abbreviation": "Navy",
            "foia_email": "DONFOIA-PA@navy.mil",
            "foia_address": "Department of the Navy\nChief of Naval Operations (DNS-36)\n2000 Navy Pentagon\nWashington, DC 20350-2000",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "Department of the Air Force",
            "abbreviation": "USAF",
            "foia_email": "usaf.pentagon.saf-aa.mbx.haf-foia-workflow@mail.mil",
            "foia_address": "Secretary of the Air Force\nHAF FOIA Office (SAF/AAII(F))\n1000 Air Force Pentagon\nWashington, DC 20330-1000",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        
        # Regulatory
        {
            "name": "Environmental Protection Agency",
            "abbreviation": "EPA",
            "foia_email": "hq.foia@epa.gov",
            "foia_address": "National FOIA Office\nU.S. EPA, MC 2822T\n1200 Pennsylvania Avenue, NW\nWashington, DC 20460",
            "foia_portal_url": "https://www.epa.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Communications Commission",
            "abbreviation": "FCC",
            "foia_email": "foia@fcc.gov",
            "foia_address": "Federal Communications Commission\nFOIA Control Office\n445 12th Street SW\nWashington, DC 20554",
            "foia_portal_url": "https://www.fcc.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Trade Commission",
            "abbreviation": "FTC",
            "foia_email": "foia@ftc.gov",
            "foia_address": "Office of General Counsel\nFederal Trade Commission\n600 Pennsylvania Avenue, NW\nWashington, DC 20580",
            "foia_portal_url": "https://www.ftc.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Securities and Exchange Commission",
            "abbreviation": "SEC",
            "foia_email": "foiapa@sec.gov",
            "foia_address": "FOIA/PA Branch, Office of FOIA Services\nSecurities and Exchange Commission\n100 F Street NE\nWashington, DC 20549",
            "foia_portal_url": "https://www.sec.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Election Commission",
            "abbreviation": "FEC",
            "foia_email": "FOIA@fec.gov",
            "foia_address": "Federal Election Commission\nFOIA Requester Service Center\n1050 First Street NE\nWashington, DC 20463",
            "foia_portal_url": "https://www.fec.gov/freedom-information-act/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Energy Regulatory Commission",
            "abbreviation": "FERC",
            "foia_email": "foia-ceii@ferc.gov",
            "foia_address": "FOIA Officer\nFederal Energy Regulatory Commission\n888 First Street NE, Room 9A-01\nWashington, DC 20426",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        
        # Executive
        {
            "name": "White House Office",
            "abbreviation": "EOP",
            "foia_address": "Executive Office of the President\nOffice of Administration\nFOIA/Privacy Act Officer\n725 17th Street NW\nWashington, DC 20503",
            "preferred_method": "mail",
            "typical_response_days": 45,
        },
        {
            "name": "Office of Management and Budget",
            "abbreviation": "OMB",
            "foia_email": "ombfoia@omb.eop.gov",
            "foia_address": "Office of Management and Budget\nFOIA Officer\n725 17th Street NW\nWashington, DC 20503",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        
        # Health & Human Services
        {
            "name": "Department of Health and Human Services",
            "abbreviation": "HHS",
            "foia_email": "osfoia@hhs.gov",
            "foia_address": "U.S. Department of Health and Human Services\nOffice of the Secretary\nFOIA Office\n330 C Street SW, Room L-110\nWashington, DC 20201",
            "foia_portal_url": "https://www.hhs.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Food and Drug Administration",
            "abbreviation": "FDA",
            "foia_email": "FDAFOIA@fda.hhs.gov",
            "foia_address": "Food and Drug Administration\nDivision of Freedom of Information\n5630 Fishers Lane, Room 1035\nRockville, MD 20857",
            "foia_portal_url": "https://www.fda.gov/regulatory-information/freedom-information",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Centers for Disease Control and Prevention",
            "abbreviation": "CDC",
            "foia_email": "cdcfoia@cdc.gov",
            "foia_address": "CDC/ATSDR\nFOIA Office, MS D-54\n1600 Clifton Road NE\nAtlanta, GA 30329-4018",
            "foia_portal_url": "https://www.cdc.gov/od/foia/",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "National Institutes of Health",
            "abbreviation": "NIH",
            "foia_email": "nihfoia@nih.gov",
            "foia_address": "National Institutes of Health\nFOIA Office\n9000 Rockville Pike, Building 31, Room 5B-35\nBethesda, MD 20892",
            "foia_portal_url": "https://www.nih.gov/institutes-nih/nih-office-director/office-communications-public-liaison/freedom-information-act-office",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Centers for Medicare & Medicaid Services",
            "abbreviation": "CMS",
            "foia_email": "cmsfoia@cms.hhs.gov",
            "foia_address": "CMS FOIA Group\n7500 Security Boulevard, N2-20-16\nBaltimore, MD 21244",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        
        # Treasury & Finance
        {
            "name": "Department of the Treasury",
            "abbreviation": "Treasury",
            "foia_email": "FOIA@treasury.gov",
            "foia_address": "Department of the Treasury\nFOIA and Transparency\n1500 Pennsylvania Avenue NW\nWashington, DC 20220",
            "foia_portal_url": "https://home.treasury.gov/footer/freedom-of-information-act",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Internal Revenue Service",
            "abbreviation": "IRS",
            "foia_email": "FOIA.Request@irs.gov",
            "foia_address": "IRS Headquarters FOIA\nStop 211\nPO Box 621506\nAtlanta, GA 30362-3006",
            "foia_portal_url": "https://www.irs.gov/privacy-disclosure/irs-freedom-of-information",
            "preferred_method": "email",
            "typical_response_days": 30,
        },
        {
            "name": "Federal Reserve System",
            "abbreviation": "FRS",
            "foia_email": "FOIA@frb.gov",
            "foia_address": "Board of Governors of the Federal Reserve System\nFOIA Office\n20th Street & Constitution Avenue NW\nWashington, DC 20551",
            "foia_portal_url": "https://www.federalreserve.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Consumer Financial Protection Bureau",
            "abbreviation": "CFPB",
            "foia_email": "FOIA@consumerfinance.gov",
            "foia_address": "CFPB FOIA Office\n1700 G Street NW\nWashington, DC 20552",
            "foia_portal_url": "https://www.consumerfinance.gov/foia-requests/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        
        # State & International
        {
            "name": "Department of State",
            "abbreviation": "State",
            "foia_email": "FOIA@state.gov",
            "foia_address": "U.S. Department of State\nOffice of Information Programs and Services\nA/GIS/IPS/RL\nSA-2, Suite 8100\nWashington, DC 20522-0208",
            "foia_portal_url": "https://foia.state.gov/",
            "preferred_method": "email",
            "typical_response_days": 35,
        },
        {
            "name": "U.S. Agency for International Development",
            "abbreviation": "USAID",
            "foia_email": "foia@usaid.gov",
            "foia_address": "USAID FOIA Office\n1300 Pennsylvania Avenue NW, Room 2.07C\nWashington, DC 20523",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        
        # Labor & Commerce
        {
            "name": "Department of Labor",
            "abbreviation": "DOL",
            "foia_email": "foiacoordinator@dol.gov",
            "foia_address": "U.S. Department of Labor\nOffice of the Solicitor\nFOIA/FACA Division\n200 Constitution Avenue NW, Room N-2428\nWashington, DC 20210",
            "foia_portal_url": "https://www.dol.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of Commerce",
            "abbreviation": "Commerce",
            "foia_email": "eFOIA@doc.gov",
            "foia_address": "Department of Commerce\nFOIA Officer\n1401 Constitution Avenue NW, Room 4513\nWashington, DC 20230",
            "foia_portal_url": "https://www.commerce.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "U.S. Patent and Trademark Office",
            "abbreviation": "USPTO",
            "foia_email": "foia@uspto.gov",
            "foia_address": "USPTO FOIA Office\nP.O. Box 1450\nAlexandria, VA 22313-1450",
            "foia_portal_url": "https://www.uspto.gov/learning-and-resources/ip-policy/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "National Oceanic and Atmospheric Administration",
            "abbreviation": "NOAA",
            "foia_email": "foia@noaa.gov",
            "foia_address": "NOAA FOIA Officer\n1315 East-West Highway, SSMC3, Room 3627\nSilver Spring, MD 20910",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        
        # Transportation
        {
            "name": "Department of Transportation",
            "abbreviation": "DOT",
            "foia_email": "foia@dot.gov",
            "foia_address": "Office of the General Counsel\nFOIA Office\n1200 New Jersey Avenue SE\nWashington, DC 20590",
            "foia_portal_url": "https://www.transportation.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Federal Aviation Administration",
            "abbreviation": "FAA",
            "foia_email": "9-AWA-ARC-FOIA@faa.gov",
            "foia_address": "FAA FOIA Office\n800 Independence Avenue SW, Room 305\nWashington, DC 20591",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "National Highway Traffic Safety Administration",
            "abbreviation": "NHTSA",
            "foia_email": "NHTSA.FOIA@dot.gov",
            "foia_address": "NHTSA FOIA Office\n1200 New Jersey Avenue SE, West Building\nWashington, DC 20590",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        
        # Other Major Agencies
        {
            "name": "National Aeronautics and Space Administration",
            "abbreviation": "NASA",
            "foia_email": "hq-foia@nasa.gov",
            "foia_address": "NASA Headquarters\nFOIA Office\n300 E Street SW, Room 5K39\nWashington, DC 20546",
            "foia_portal_url": "https://www.nasa.gov/FOIA/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Social Security Administration",
            "abbreviation": "SSA",
            "foia_email": "foia.pa.officers@ssa.gov",
            "foia_address": "Social Security Administration\nOffice of Privacy and Disclosure\nFOIA Workgroup\n617 Altmeyer Building\n6401 Security Boulevard\nBaltimore, MD 21235",
            "foia_portal_url": "https://www.ssa.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Department of Education",
            "abbreviation": "ED",
            "foia_email": "EDFOIAManager@ed.gov",
            "foia_address": "U.S. Department of Education\nFOIA Service Center\n400 Maryland Avenue SW, LBJ 7W106A\nWashington, DC 20202",
            "foia_portal_url": "https://www2.ed.gov/policy/gen/leg/foia/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of Energy",
            "abbreviation": "DOE",
            "foia_email": "FOIA-Central@hq.doe.gov",
            "foia_address": "FOIA Officer\nU.S. Department of Energy\n1000 Independence Avenue SW\nWashington, DC 20585",
            "foia_portal_url": "https://www.energy.gov/management/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of Veterans Affairs",
            "abbreviation": "VA",
            "foia_email": "vaborefoia@va.gov",
            "foia_address": "Department of Veterans Affairs\nFOIA Service (005R1C)\n810 Vermont Avenue NW\nWashington, DC 20420",
            "foia_portal_url": "https://www.va.gov/foia/",
            "preferred_method": "email",
            "typical_response_days": 25,
        },
        {
            "name": "Department of Housing and Urban Development",
            "abbreviation": "HUD",
            "foia_email": "HUD_FOIA@hud.gov",
            "foia_address": "HUD FOIA Office\n451 7th Street SW, Room 10139\nWashington, DC 20410",
            "foia_portal_url": "https://www.hud.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of the Interior",
            "abbreviation": "DOI",
            "foia_email": "os_foia@ios.doi.gov",
            "foia_address": "Department of the Interior\nFOIA Officer\n1849 C Street NW, MS-7328-MIB\nWashington, DC 20240",
            "foia_portal_url": "https://www.doi.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Department of Agriculture",
            "abbreviation": "USDA",
            "foia_email": "APHIS.FOIA.Officer@usda.gov",
            "foia_address": "USDA FOIA Service Center\n1400 Independence Avenue SW, Room 4037A\nWashington, DC 20250",
            "foia_portal_url": "https://www.usda.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        
        # Independent Agencies
        {
            "name": "General Services Administration",
            "abbreviation": "GSA",
            "foia_email": "gsa.foia@gsa.gov",
            "foia_address": "GSA FOIA Requester Service Center (H1F)\n1800 F Street NW\nWashington, DC 20405",
            "foia_portal_url": "https://www.gsa.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Office of Personnel Management",
            "abbreviation": "OPM",
            "foia_email": "foia@opm.gov",
            "foia_address": "U.S. Office of Personnel Management\nFOIA Requester Service Center\n1900 E Street NW, Room 5415\nWashington, DC 20415-0001",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "National Archives and Records Administration",
            "abbreviation": "NARA",
            "foia_email": "foia@nara.gov",
            "foia_address": "National Archives and Records Administration\nFOIA Office\n8601 Adelphi Road, Room 3110\nCollege Park, MD 20740-6001",
            "foia_portal_url": "https://www.archives.gov/foia",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Small Business Administration",
            "abbreviation": "SBA",
            "foia_email": "foia@sba.gov",
            "foia_address": "SBA FOIA Office\n409 3rd Street SW\nWashington, DC 20416",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "U.S. Postal Service",
            "abbreviation": "USPS",
            "foia_email": "foia@usps.gov",
            "foia_address": "Records Office\nFOIA Requester Service Center\nU.S. Postal Service\n475 L'Enfant Plaza SW, Room 1P830\nWashington, DC 20260-1101",
            "foia_portal_url": "https://about.usps.com/who/legal/foia/",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "National Labor Relations Board",
            "abbreviation": "NLRB",
            "foia_email": "foia@nlrb.gov",
            "foia_address": "National Labor Relations Board\nFOIA Officer\n1015 Half Street SE\nWashington, DC 20570",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
        {
            "name": "Equal Employment Opportunity Commission",
            "abbreviation": "EEOC",
            "foia_email": "foia@eeoc.gov",
            "foia_address": "Equal Employment Opportunity Commission\nFOIA Programs\n131 M Street NE\nWashington, DC 20507",
            "preferred_method": "email",
            "typical_response_days": 20,
        },
    ]
