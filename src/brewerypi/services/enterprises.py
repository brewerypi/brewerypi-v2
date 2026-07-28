"""Service-layer CRUD for enterprises (top of the hierarchy).

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from brewerypi.models import (
    Area,
    Enterprise,
    Lookup,
    LookupValue,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services._validation import clean_str, optional_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_enterprises(session: Session) -> list[Enterprise]:
    """Return all enterprises."""
    return list(session.scalars(select(Enterprise).order_by(Enterprise.name)))


def get_enterprise(session: Session, enterprise_id: int) -> Enterprise:
    """Return one enterprise, or raise NotFoundError."""
    ent = session.get(Enterprise, enterprise_id)
    if ent is None:
        raise NotFoundError(f"no enterprise with id {enterprise_id}")
    return ent


def create_enterprise(
    session: Session,
    abbreviation: str,
    name: str,
    description: str | None = None,
) -> Enterprise:
    """Create an enterprise (abbreviation and name are globally unique)."""
    abbreviation = clean_str(abbreviation, "abbreviation", 10)
    name = clean_str(name, "name", 45)
    _check_unique(session, abbreviation, name)
    ent = Enterprise(
        abbreviation=abbreviation,
        name=name,
        description=optional_str(description),
    )
    session.add(ent)
    session.flush()
    return ent


def update_enterprise(
    session: Session,
    enterprise_id: int,
    abbreviation: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> Enterprise:
    """Update an enterprise; only provided fields change."""
    ent = get_enterprise(session, enterprise_id)
    new_abbr = ent.abbreviation
    new_name = ent.name
    if abbreviation is not None:
        new_abbr = clean_str(abbreviation, "abbreviation", 10)
    if name is not None:
        new_name = clean_str(name, "name", 45)
    _check_unique(session, new_abbr, new_name, exclude_id=enterprise_id)
    ent.abbreviation = new_abbr
    ent.name = new_name
    if description is not None:
        ent.description = optional_str(description)
    session.flush()
    return ent


def delete_enterprise(session: Session, enterprise_id: int) -> None:
    """Delete an enterprise and its whole subtree, guarding history.

    An enterprise cascades to its sites -> areas -> tags -> tag_values and to
    its lookups -> lookup_values and measurement_units. This refuses if any
    recorded reading exists under its sites, or (defense in depth) if any of
    its lookup values are referenced by a reading -- either of which would
    otherwise destroy or block on historical data.
    """
    ent = get_enterprise(session, enterprise_id)
    readings = session.scalar(
        select(func.count())
        .select_from(TagValue)
        .join(Tag, TagValue.tag_id == Tag.id)
        .join(Area, Tag.area_id == Area.id)
        .join(Site, Area.site_id == Site.id)
        .where(Site.enterprise_id == enterprise_id)
    )
    if readings:
        raise ValidationError(
            f"cannot delete enterprise {enterprise_id}: {readings} "
            "recorded reading(s) exist under its sites"
        )
    referenced = session.scalar(
        select(func.count())
        .select_from(TagValue)
        .join(LookupValue, TagValue.lookup_value_id == LookupValue.id)
        .join(Lookup, LookupValue.lookup_id == Lookup.id)
        .where(Lookup.enterprise_id == enterprise_id)
    )
    if referenced:
        raise ValidationError(
            f"cannot delete enterprise {enterprise_id}: {referenced} "
            "reading(s) reference its lookup values"
        )
    session.delete(ent)
    session.flush()


def _check_unique(
    session: Session,
    abbreviation: str,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Raise ConflictError if abbreviation or name is already taken."""
    stmt = select(Enterprise).where(
        or_(
            Enterprise.abbreviation == abbreviation,
            Enterprise.name == name,
        )
    )
    if exclude_id is not None:
        stmt = stmt.where(Enterprise.id != exclude_id)
    existing = session.scalars(stmt).first()
    if existing is not None:
        field = (
            "abbreviation"
            if existing.abbreviation == abbreviation
            else "name"
        )
        raise ConflictError(
            f"an enterprise with that {field} already exists"
        )
