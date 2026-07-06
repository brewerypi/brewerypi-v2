"""Service-layer read/update/delete for tag values (recorded readings).

Readings are the historian's time-series data. Creation is handled by the
operator-tier record_tag_value; update and delete here are corrective
operations (fix or remove a bad reading), intended for the admin tier.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi.models import LookupValue, Tag, TagValue
from brewerypi.services.exceptions import NotFoundError, ValidationError


def get_tag_value(session: Session, value_id: int) -> TagValue:
    """Return one recorded reading, or raise NotFoundError."""
    tv = session.get(TagValue, value_id)
    if tv is None:
        raise NotFoundError(f"no tag value with id {value_id}")
    return tv


def update_tag_value(
    session: Session,
    value_id: int,
    value: float | None = None,
    lookup_value: str | None = None,
    timestamp: str | None = None,
) -> TagValue:
    """Correct a reading's value and/or timestamp.

    The value kind must match the reading's tag: numeric tags take ``value``,
    lookup-typed tags take ``lookup_value`` (the name of a selectable value).
    A reading's type cannot be switched. Passing nothing is a no-op.
    """
    tv = get_tag_value(session, value_id)
    tag = session.get(Tag, tv.tag_id)
    if tag.lookup_id is not None:
        if value is not None:
            raise ValidationError(
                "this reading is on a lookup-typed tag; update "
                "lookup_value, not value"
            )
        if lookup_value is not None:
            tv.lookup_value_id = _resolve_lookup_value(
                session, tag.lookup_id, lookup_value
            )
    else:
        if lookup_value is not None:
            raise ValidationError(
                "this reading is on a numeric tag; update value, not "
                "lookup_value"
            )
        if value is not None:
            tv.value = value
    if timestamp is not None:
        tv.timestamp = _parse_timestamp(timestamp)
    session.flush()
    return tv


def delete_tag_value(session: Session, value_id: int) -> None:
    """Delete a single recorded reading."""
    tv = get_tag_value(session, value_id)
    session.delete(tv)
    session.flush()


def _resolve_lookup_value(
    session: Session, lookup_id: int, name: str
) -> int:
    lv = session.scalars(
        select(LookupValue).where(
            LookupValue.lookup_id == lookup_id,
            LookupValue.name == name,
            LookupValue.is_selectable.is_(True),
        )
    ).first()
    if lv is None:
        allowed = session.scalars(
            select(LookupValue.name).where(
                LookupValue.lookup_id == lookup_id,
                LookupValue.is_selectable.is_(True),
            )
        ).all()
        raise ValidationError(
            f"{name!r} is not a selectable value for this tag; "
            f"allowed: {sorted(allowed)}"
        )
    return lv.id


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            f"invalid timestamp {value!r}; use ISO 8601"
        ) from exc
