"""Service-layer CRUD for lookup values.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brewerypi.models import (
    EventFrameAttributeTemplate,
    Lookup,
    LookupValue,
    TagValue,
)
from brewerypi.services._validation import clean_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_lookup_values(
    session: Session, lookup_id: int
) -> list[LookupValue]:
    """Return the values belonging to a lookup."""
    stmt = (
        select(LookupValue)
        .where(LookupValue.lookup_id == lookup_id)
        .order_by(LookupValue.name)
    )
    return list(session.scalars(stmt).all())


def get_lookup_value(session: Session, value_id: int) -> LookupValue:
    """Return one lookup value, or raise NotFoundError."""
    value = session.get(LookupValue, value_id)
    if value is None:
        raise NotFoundError(f"no lookup value with id {value_id}")
    return value


def create_lookup_value(
    session: Session,
    lookup_id: int,
    name: str,
    is_selectable: bool = True,
) -> LookupValue:
    """Create a value under a lookup (name unique within the lookup)."""
    name = clean_str(name, "name", 45)
    if session.get(Lookup, lookup_id) is None:
        raise NotFoundError(f"no lookup with id {lookup_id}")
    _check_unique(session, lookup_id, name)
    value = LookupValue(
        lookup_id=lookup_id, name=name, is_selectable=is_selectable
    )
    session.add(value)
    session.flush()
    return value


def update_lookup_value(
    session: Session,
    value_id: int,
    name: str | None = None,
    is_selectable: bool | None = None,
) -> LookupValue:
    """Update a lookup value; only provided fields change."""
    value = get_lookup_value(session, value_id)
    if name is not None:
        new_name = clean_str(name, "name", 45)
        _check_unique(
            session, value.lookup_id, new_name, exclude_id=value_id
        )
        value.name = new_name
    if is_selectable is not None:
        value.is_selectable = is_selectable
    session.flush()
    return value


def delete_lookup_value(session: Session, value_id: int) -> None:
    """Delete a lookup value, refusing if any reading references it."""
    value = get_lookup_value(session, value_id)
    refs = session.scalar(
        select(func.count())
        .select_from(TagValue)
        .where(TagValue.lookup_value_id == value_id)
    )
    if refs:
        raise ValidationError(
            f"cannot delete lookup value {value_id}: "
            f"{refs} recorded reading(s) reference it"
        )
    default_refs = session.scalar(
        select(func.count())
        .select_from(EventFrameAttributeTemplate)
        .where(
            (
                EventFrameAttributeTemplate.default_start_lookup_value_id
                == value_id
            )
            | (
                EventFrameAttributeTemplate.default_end_lookup_value_id
                == value_id
            )
        )
    )
    if default_refs:
        raise ValidationError(
            f"cannot delete lookup value {value_id}: "
            f"{default_refs} event frame attribute template default(s) "
            "reference it"
        )
    session.delete(value)
    session.flush()


def _check_unique(
    session: Session,
    lookup_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Raise ConflictError if the name is taken within the lookup."""
    stmt = select(LookupValue).where(
        LookupValue.lookup_id == lookup_id,
        LookupValue.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(LookupValue.id != exclude_id)
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"a value named {name!r} already exists in lookup {lookup_id}"
        )
