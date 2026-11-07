"""Service-layer record/read/update/delete for tag values (readings).

Readings are the historian's time-series data. Recording is an operator
write; update and delete are corrective operations (fix or remove a bad
reading), on the operator tier so operators can fix mis-entries. The MCP
tools and the web app both go through these, so the rules live here once.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brewerypi.models import LookupValue, Tag, TagValue
from brewerypi.services.exceptions import (
    NotFoundError,
    NotSelectableError,
    ValidationError,
)


def record_tag_value(
    session: Session,
    tag_id: int,
    observed_at: datetime,
    value: float | None = None,
    lookup_value: str | None = None,
    recorded_by_id: int | None = None,
) -> TagValue:
    """Record one reading at ``observed_at`` (naive UTC).

    A numeric tag takes ``value``; a lookup-typed tag takes ``lookup_value``,
    the name of a selectable option on its list. Exactly one of the two.
    """
    tag = session.get(Tag, tag_id)
    if tag is None:
        raise NotFoundError(f"no tag with id {tag_id}")
    if tag.lookup_id is not None:
        if lookup_value is None or value is not None:
            raise ValidationError(
                f"tag {tag_id} ({tag.name}) is lookup-typed; pass only "
                "'lookup_value'"
            )
        reading = TagValue(
            tag_id=tag_id,
            observed_at=observed_at,
            lookup_value_id=_resolve_lookup_value(
                session, tag.lookup_id, lookup_value
            ),
            recorded_by_id=recorded_by_id,
        )
    else:
        if value is None or lookup_value is not None:
            raise ValidationError(
                f"tag {tag_id} ({tag.name}) is numeric; pass only 'value'"
            )
        reading = TagValue(
            tag_id=tag_id,
            observed_at=observed_at,
            value=value,
            recorded_by_id=recorded_by_id,
        )
    session.add(reading)
    session.flush()
    return reading


def list_tag_values(
    session: Session,
    tag_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
    oldest_first: bool = False,
) -> list[TagValue]:
    """A tag's readings, newest first, between optional UTC bounds.

    Both bounds are inclusive, so a batch's window takes in the end value
    written at its close. Ties on time break on id, so paging with ``limit``
    and ``offset`` never repeats or skips a reading.
    """
    if oldest_first:
        order = (TagValue.observed_at.asc(), TagValue.id.asc())
    else:
        order = (TagValue.observed_at.desc(), TagValue.id.desc())
    stmt = _bounded(tag_id, start, end, select(TagValue)).order_by(*order)
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def count_tag_values(
    session: Session,
    tag_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """How many readings a tag has between optional inclusive UTC bounds."""
    stmt = _bounded(tag_id, start, end, select(func.count(TagValue.id)))
    return session.scalar(stmt)


def _bounded(tag_id: int, start, end, stmt):
    stmt = stmt.where(TagValue.tag_id == tag_id)
    if start is not None:
        stmt = stmt.where(TagValue.observed_at >= start)
    if end is not None:
        stmt = stmt.where(TagValue.observed_at <= end)
    return stmt


def selectable_values(session: Session, lookup_id: int) -> list[str]:
    """The options a reading on this list may take, by name."""
    return list(
        session.scalars(
            select(LookupValue.name)
            .where(
                LookupValue.lookup_id == lookup_id,
                LookupValue.is_selectable.is_(True),
            )
            .order_by(LookupValue.name)
        ).all()
    )


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
    observed_at: str | None = None,
    updated_by_id: int | None = None,
) -> TagValue:
    """Correct a reading's value and/or observed_at.

    The value kind must match the reading's tag: numeric tags take ``value``,
    lookup-typed tags take ``lookup_value`` (the name of a selectable value).
    A reading's type cannot be switched. Passing nothing is a no-op, and so
    is changing nothing: only a real change records ``updated_by_id``.
    """
    tv = get_tag_value(session, value_id)
    before = (tv.value, tv.lookup_value_id, tv.observed_at)
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
    if observed_at is not None:
        tv.observed_at = _parse_timestamp(observed_at)
    if (tv.value, tv.lookup_value_id, tv.observed_at) != before:
        tv.updated_by_id = updated_by_id
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
        allowed = selectable_values(session, lookup_id)
        raise NotSelectableError(
            f"{name!r} is not a selectable value for this tag; "
            f"allowed: {sorted(allowed)}",
            allowed,
        )
    return lv.id


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            f"invalid timestamp {value!r}; use ISO 8601"
        ) from exc
