"""Active-case bookkeeping for investigations (see models.Case)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from .db import get_data_dir
from .models import Case


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "case"


def _active_case_file() -> Path:
    return get_data_dir() / "active_case.json"


def get_active_case_id() -> str | None:
    # Only the case id is stored here: this file sits outside the encrypted
    # database, so it must not carry the investigation's name.
    try:
        return json.loads(_active_case_file().read_text()).get("id")
    except (OSError, ValueError, AttributeError):
        return None


def set_active_case_id(case_id: str) -> None:
    _active_case_file().write_text(json.dumps({"id": case_id}))


def clear_active_case() -> None:
    _active_case_file().unlink(missing_ok=True)


def get_active_case(session: Session) -> Case | None:
    """The active case, or None if unset or absent from this database."""
    case_id = get_active_case_id()
    return session.get(Case, case_id) if case_id else None


def find_case(session: Session, name_or_slug: str) -> Case | None:
    return (
        session.query(Case)
        .filter(
            (Case.slug == slugify(name_or_slug))
            | (func.lower(Case.name) == name_or_slug.strip().lower())
        )
        .first()
    )


def active_case_id_in_db() -> str | None:
    """Active case id if a database exists and holds that case, else None."""
    from .db import get_db_path, get_session

    if not get_active_case_id() or not get_db_path().exists():
        return None
    with get_session() as session:
        case = get_active_case(session)
        return case.id if case else None
