"""Measurements as the pages show them: a name, a unit, the latest value.

A tag is reached three ways. Equipment lists its element attributes, a
batch type's attributes belong to batches, and an area holds tags that
belong to neither (continuous data such as a utility meter). Whichever way,
the measurement page names it after the attribute when there is one, and
its breadcrumb runs through the equipment or the area it sits in.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi import access, services
from brewerypi.identity import CurrentUser
from brewerypi.models import (
    Area,
    Element,
    ElementAttribute,
    ElementAttributeTemplate,
    EventFrameAttribute,
    EventFrameAttributeTemplate,
    LookupValue,
    Site,
    Tag,
    TagValue,
)
from brewerypi.timezones import from_utc, zone_for_tag
from brewerypi.web.navigation import HOME, trail
from brewerypi.web.rendering import local_time


def number(value: float) -> str:
    """A stored number as typed: 18.5 stays 18.5, and 64.0 shows as 64."""
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def unit_of(tag: Tag) -> str | None:
    unit = tag.measurement_unit
    return unit.abbreviation if unit is not None else None


def raw_value(session: Session, tv: TagValue) -> str:
    """A reading's value as a form field holds it: no unit."""
    if tv.lookup_value_id is not None:
        option = session.get(LookupValue, tv.lookup_value_id)
        return option.name if option is not None else ""
    return number(tv.value)


def value_text(session: Session, tag: Tag, tv: TagValue) -> str:
    """A reading's value as people read it, with its unit."""
    text = raw_value(session, tv)
    unit = unit_of(tag)
    if tv.lookup_value_id is None and unit:
        return f"{text} {unit}"
    return text


def owner(session: Session, tag: Tag) -> tuple[Element | None, str]:
    """The equipment a tag belongs to, and the measurement's name there.

    An adopted tag can be shared by several attributes; the first wired
    names it. A tag belonging to nothing keeps its own name.
    """
    attribute = session.scalars(
        select(ElementAttribute)
        .where(ElementAttribute.tag_id == tag.id)
        .order_by(ElementAttribute.id)
    ).first()
    if attribute is not None:
        template = session.get(
            ElementAttributeTemplate, attribute.element_attribute_template_id
        )
        return session.get(Element, attribute.element_id), template.name
    batch_attribute = session.scalars(
        select(EventFrameAttribute)
        .where(EventFrameAttribute.tag_id == tag.id)
        .order_by(EventFrameAttribute.id)
    ).first()
    if batch_attribute is not None:
        template = session.get(
            EventFrameAttributeTemplate,
            batch_attribute.event_frame_attribute_template_id,
        )
        return session.get(Element, batch_attribute.element_id), template.name
    return None, tag.name


def tag_trail(session: Session, tag: Tag) -> list[dict]:
    """The breadcrumb down to the equipment or area a tag sits in."""
    element, _ = owner(session, tag)
    if element is not None:
        return trail(session, element)
    area = session.get(Area, tag.area_id)
    site = session.get(Site, area.site_id)
    return [
        HOME,
        {"href": f"/sites/{site.id}", "name": site.name},
        {"href": f"/areas/{area.id}", "name": area.name},
    ]


def measurement(session: Session, tag: Tag, name: str) -> dict:
    """One measurement as a list shows it, with its latest reading."""
    latest = services.list_tag_values(session, tag.id, limit=1)
    zone = zone_for_tag(session, tag)
    return {
        "id": tag.id,
        "name": name,
        "unit": unit_of(tag),
        "latest": (
            {
                "value": value_text(session, tag, latest[0]),
                "at": local_time(from_utc(latest[0].observed_at, zone)),
            }
            if latest
            else None
        ),
    }


def element_measurements(
    session: Session, element: Element
) -> list[dict]:
    """A piece of equipment's measurements, by name.

    Reading the equipment means reading its tags: they sit at its site.
    """
    rows = []
    for attribute in services.list_element_attributes(
        session, element_id=element.id
    ):
        template = session.get(
            ElementAttributeTemplate, attribute.element_attribute_template_id
        )
        tag = session.get(Tag, attribute.tag_id)
        rows.append(measurement(session, tag, template.name))
    return sorted(rows, key=lambda m: m["name"].lower())


def areas_with_measurements(
    session: Session, user: CurrentUser, site: Site
) -> list[dict]:
    """The site's areas that hold measurements of their own, with counts."""
    areas = access.visible(
        session,
        user,
        session.scalars(
            select(Area).where(Area.site_id == site.id).order_by(Area.name)
        ),
    )
    out = []
    for area in areas:
        count = len(services.list_standalone_tags(session, area.id))
        if count:
            out.append({"id": area.id, "name": area.name, "count": count})
    return out
