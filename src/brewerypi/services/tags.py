"""Service-layer CRUD for tags.

A tag belongs to an area and is either lookup-typed (``lookup_id`` set) or
numeric (``measurement_unit_id`` set, or neither) — never both. Any lookup
or measurement unit it references must belong to the tag's own enterprise.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi.models import (
    Area,
    Lookup,
    MeasurementUnit,
    Site,
    Tag,
)
from brewerypi.services._validation import clean_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_tags(
    session: Session, area_id: int | None = None
) -> list[Tag]:
    """Return tags, optionally filtered by area."""
    stmt = select(Tag).order_by(Tag.name)
    if area_id is not None:
        stmt = stmt.where(Tag.area_id == area_id)
    return list(session.scalars(stmt).all())


def get_tag(session: Session, tag_id: int) -> Tag:
    """Return one tag, or raise NotFoundError."""
    tag = session.get(Tag, tag_id)
    if tag is None:
        raise NotFoundError(f"no tag with id {tag_id}")
    return tag


def create_tag(
    session: Session,
    area_id: int,
    name: str,
    description: str | None = None,
    lookup_id: int | None = None,
    measurement_unit_id: int | None = None,
) -> Tag:
    """Create a tag under an area.

    Validates the area exists, that the name is unique within the area, that
    the tag is not both lookup-typed and numeric, and that any referenced
    lookup or measurement unit belongs to the area's enterprise.
    """
    name = clean_str(name, "name", 255)
    enterprise_id = _area_enterprise_id(session, area_id)
    if lookup_id is not None and measurement_unit_id is not None:
        raise ValidationError(
            "a tag is either lookup-typed or numeric; provide lookup_id "
            "or measurement_unit_id, not both"
        )
    _check_lookup(session, lookup_id, enterprise_id)
    _check_measurement_unit(session, measurement_unit_id, enterprise_id)
    _check_unique(session, area_id, name)
    tag = Tag(
        area_id=area_id,
        name=name,
        description=_optional(description),
        lookup_id=lookup_id,
        measurement_unit_id=measurement_unit_id,
    )
    session.add(tag)
    session.flush()
    return tag


def update_tag(
    session: Session,
    tag_id: int,
    name: str | None = None,
    description: str | None = None,
) -> Tag:
    """Update a tag's name and/or description.

    Changing a tag's type (lookup vs numeric) is intentionally not supported
    here, since existing readings would become inconsistent; delete and
    recreate an unused tag instead.
    """
    tag = get_tag(session, tag_id)
    if name is not None:
        new_name = clean_str(name, "name", 255)
        _check_unique(session, tag.area_id, new_name, exclude_id=tag_id)
        tag.name = new_name
    if description is not None:
        tag.description = _optional(description)
    session.flush()
    return tag


def delete_tag(session: Session, tag_id: int) -> None:
    """Delete a tag and its recorded readings.

    Readings go with the tag: ``tag_values.tag_id`` is NOT NULL, so they
    cannot outlive it, and the relationship cascades. Callers that expose this
    should confirm first -- the MCP tool previews the reading count and date
    range and requires ``confirm=true``.

    Refuses while an element or event frame attribute is still wired to the
    tag; unwire those first (the foreign key is RESTRICT, so this turns what
    would be a raw integrity error into a clear message).
    """
    tag = get_tag(session, tag_id)
    from brewerypi.services.element_attributes import tag_is_referenced

    if tag_is_referenced(session, tag_id):
        raise ValidationError(
            f"cannot delete tag {tag_id}: an element or event frame "
            "attribute is still wired to it; unwire it first"
        )
    session.delete(tag)
    session.flush()


def _area_enterprise_id(session: Session, area_id: int) -> int:
    """Return the enterprise id an area belongs to (validates the area)."""
    area = session.get(Area, area_id)
    if area is None:
        raise NotFoundError(f"no area with id {area_id}")
    site = session.get(Site, area.site_id)
    return site.enterprise_id


def _check_lookup(
    session: Session, lookup_id: int | None, enterprise_id: int
) -> None:
    if lookup_id is None:
        return
    lookup = session.get(Lookup, lookup_id)
    if lookup is None:
        raise NotFoundError(f"no lookup with id {lookup_id}")
    if lookup.enterprise_id != enterprise_id:
        raise ValidationError(
            f"lookup {lookup_id} belongs to a different enterprise"
        )


def _check_measurement_unit(
    session: Session, unit_id: int | None, enterprise_id: int
) -> None:
    if unit_id is None:
        return
    unit = session.get(MeasurementUnit, unit_id)
    if unit is None:
        raise NotFoundError(f"no measurement unit with id {unit_id}")
    if unit.enterprise_id != enterprise_id:
        raise ValidationError(
            f"measurement unit {unit_id} belongs to a different enterprise"
        )


def _check_unique(
    session: Session,
    area_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Raise ConflictError if the name is taken within the area."""
    stmt = select(Tag).where(Tag.area_id == area_id, Tag.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Tag.id != exclude_id)
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"a tag named {name!r} already exists in area {area_id}"
        )


def _optional(value: str | None) -> str | None:
    """Normalize an optional text field: blank becomes None."""
    if value is None:
        return None
    value = value.strip()
    return value or None
