"""Service-layer CRUD for areas.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from brewerypi.models import Area, Element, Site, Tag, TagValue
from brewerypi.services._validation import clean_str, optional_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_areas(
    session: Session, site_id: int | None = None
) -> list[Area]:
    """Return areas, optionally filtered by site."""
    stmt = select(Area).order_by(Area.name)
    if site_id is not None:
        stmt = stmt.where(Area.site_id == site_id)
    return list(session.scalars(stmt).all())


def get_area(session: Session, area_id: int) -> Area:
    """Return one area, or raise NotFoundError."""
    area = session.get(Area, area_id)
    if area is None:
        raise NotFoundError(f"no area with id {area_id}")
    return area


def create_area(
    session: Session,
    site_id: int,
    abbreviation: str,
    name: str,
    description: str | None = None,
) -> Area:
    """Create an area under a site (abbreviation/name unique per site)."""
    abbreviation = clean_str(abbreviation, "abbreviation", 10)
    name = clean_str(name, "name", 45)
    if session.get(Site, site_id) is None:
        raise NotFoundError(f"no site with id {site_id}")
    _check_unique(session, site_id, abbreviation, name)
    area = Area(
        site_id=site_id,
        abbreviation=abbreviation,
        name=name,
        description=optional_str(description),
    )
    session.add(area)
    session.flush()
    return area


def update_area(
    session: Session,
    area_id: int,
    abbreviation: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> Area:
    """Update an area; only provided fields change."""
    area = get_area(session, area_id)
    new_abbr = area.abbreviation
    new_name = area.name
    if abbreviation is not None:
        new_abbr = clean_str(abbreviation, "abbreviation", 10)
    if name is not None:
        new_name = clean_str(name, "name", 45)
    _check_unique(
        session, area.site_id, new_abbr, new_name, exclude_id=area_id
    )
    area.abbreviation = new_abbr
    area.name = new_name
    if description is not None:
        area.description = optional_str(description)
    session.flush()
    return area


def delete_area(session: Session, area_id: int) -> None:
    """Delete an area (and its tags), refusing if readings exist below it.

    Area -> tags -> tag_values all cascade, so a delete would destroy any
    recorded history under the area. This refuses when that history exists;
    the tags themselves (config) are removed by the cascade.
    """
    area = get_area(session, area_id)
    readings = session.scalar(
        select(func.count())
        .select_from(TagValue)
        .join(Tag, TagValue.tag_id == Tag.id)
        .where(Tag.area_id == area_id)
    )
    if readings:
        raise ValidationError(
            f"cannot delete area {area_id}: {readings} recorded "
            "reading(s) exist under its tags"
        )
    elements = session.scalar(
        select(func.count())
        .select_from(Element)
        .where(Element.tag_area_id == area_id)
    )
    if elements:
        raise ValidationError(
            f"cannot delete area {area_id}: {elements} element(s) use it "
            "as their tag area; reassign them first"
        )
    wired = _wired_tag_count(session, area_id)
    if wired:
        raise ValidationError(
            f"cannot delete area {area_id}: {wired} of its tag(s) are "
            "wired to an element or event frame attribute; unwire them "
            "first"
        )
    session.delete(area)
    session.flush()


def _wired_tag_count(session: Session, area_id: int) -> int:
    """Tags in this area that an attribute still points at.

    Manual wiring can link an attribute to a tag outside its element's own
    tag area, so the element-based check above does not catch every case;
    without this the RESTRICT foreign key would surface as a raw error.
    """
    from brewerypi.models import ElementAttribute, EventFrameAttribute

    element_wired = session.scalar(
        select(func.count())
        .select_from(ElementAttribute)
        .join(Tag, ElementAttribute.tag_id == Tag.id)
        .where(Tag.area_id == area_id)
    )
    frame_wired = session.scalar(
        select(func.count())
        .select_from(EventFrameAttribute)
        .join(Tag, EventFrameAttribute.tag_id == Tag.id)
        .where(Tag.area_id == area_id)
    )
    return (element_wired or 0) + (frame_wired or 0)


def _check_unique(
    session: Session,
    site_id: int,
    abbreviation: str,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Raise ConflictError if abbreviation or name is taken in the site."""
    stmt = select(Area).where(
        Area.site_id == site_id,
        or_(Area.abbreviation == abbreviation, Area.name == name),
    )
    if exclude_id is not None:
        stmt = stmt.where(Area.id != exclude_id)
    existing = session.scalars(stmt).first()
    if existing is not None:
        field = (
            "abbreviation"
            if existing.abbreviation == abbreviation
            else "name"
        )
        raise ConflictError(
            f"an area with that {field} already exists in site {site_id}"
        )
