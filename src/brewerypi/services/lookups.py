"""Service-layer CRUD for lookups (named sets of allowed values).

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brewerypi.models import (
    ElementAttributeTemplate,
    Enterprise,
    EventFrameAttributeTemplate,
    Lookup,
    LookupValue,
    Tag,
    TagValue,
)
from brewerypi.services._validation import clean_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_lookups(
    session: Session, enterprise_id: int | None = None
) -> list[Lookup]:
    """Return lookups, optionally filtered by enterprise."""
    stmt = select(Lookup).order_by(Lookup.name)
    if enterprise_id is not None:
        stmt = stmt.where(Lookup.enterprise_id == enterprise_id)
    return list(session.scalars(stmt).all())


def get_lookup(session: Session, lookup_id: int) -> Lookup:
    """Return one lookup, or raise NotFoundError."""
    lookup = session.get(Lookup, lookup_id)
    if lookup is None:
        raise NotFoundError(f"no lookup with id {lookup_id}")
    return lookup


def create_lookup(
    session: Session, enterprise_id: int, name: str
) -> Lookup:
    """Create a lookup under an enterprise (name unique per enterprise)."""
    name = clean_str(name, "name", 45)
    if session.get(Enterprise, enterprise_id) is None:
        raise NotFoundError(f"no enterprise with id {enterprise_id}")
    _check_unique(session, enterprise_id, name)
    lookup = Lookup(enterprise_id=enterprise_id, name=name)
    session.add(lookup)
    session.flush()
    return lookup


def update_lookup(
    session: Session, lookup_id: int, name: str | None = None
) -> Lookup:
    """Rename a lookup; only provided fields change."""
    lookup = get_lookup(session, lookup_id)
    if name is not None:
        new_name = clean_str(name, "name", 45)
        _check_unique(
            session, lookup.enterprise_id, new_name, exclude_id=lookup_id
        )
        lookup.name = new_name
    session.flush()
    return lookup


def delete_lookup(session: Session, lookup_id: int) -> None:
    """Delete a lookup and its values.

    Refuses if any tag uses the lookup, or if any recorded tag value
    references one of the lookup's values (which would otherwise block the
    cascade under ON DELETE RESTRICT).
    """
    lookup = get_lookup(session, lookup_id)
    tag_refs = session.scalar(
        select(func.count())
        .select_from(Tag)
        .where(Tag.lookup_id == lookup_id)
    )
    if tag_refs:
        raise ValidationError(
            f"cannot delete lookup {lookup_id}: "
            f"{tag_refs} tag(s) use it"
        )
    attr_refs = session.scalar(
        select(func.count())
        .select_from(ElementAttributeTemplate)
        .where(ElementAttributeTemplate.lookup_id == lookup_id)
    )
    if attr_refs:
        raise ValidationError(
            f"cannot delete lookup {lookup_id}: "
            f"{attr_refs} attribute template(s) use it"
        )
    ef_attr_refs = session.scalar(
        select(func.count())
        .select_from(EventFrameAttributeTemplate)
        .where(EventFrameAttributeTemplate.lookup_id == lookup_id)
    )
    if ef_attr_refs:
        raise ValidationError(
            f"cannot delete lookup {lookup_id}: "
            f"{ef_attr_refs} event frame attribute template(s) use it"
        )
    value_refs = session.scalar(
        select(func.count())
        .select_from(TagValue)
        .join(LookupValue, TagValue.lookup_value_id == LookupValue.id)
        .where(LookupValue.lookup_id == lookup_id)
    )
    if value_refs:
        raise ValidationError(
            f"cannot delete lookup {lookup_id}: "
            f"{value_refs} recorded reading(s) reference its values"
        )
    session.delete(lookup)
    session.flush()


def _check_unique(
    session: Session,
    enterprise_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Raise ConflictError if the name is taken within the enterprise."""
    stmt = select(Lookup).where(
        Lookup.enterprise_id == enterprise_id,
        Lookup.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(Lookup.id != exclude_id)
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"a lookup named {name!r} already exists in "
            f"enterprise {enterprise_id}"
        )
