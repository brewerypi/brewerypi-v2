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
    EventFrame,
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


def options_of(session: Session, tag: Tag) -> list[str] | None:
    """A list measurement's options to choose from; None for a number."""
    if tag.lookup_id is None:
        return None
    return services.selectable_values(session, tag.lookup_id)


def measurement(session: Session, tag: Tag, name: str) -> dict:
    """One measurement as a list shows it, with its latest reading."""
    latest = services.list_tag_values(session, tag.id, limit=1)
    zone = zone_for_tag(session, tag)
    return {
        "id": tag.id,
        "name": name,
        "unit": unit_of(tag),
        "options": options_of(session, tag),
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


def batch_tag(session: Session, frame: EventFrame, tag: Tag) -> bool:
    """Whether a tag holds one of this batch's measurements.

    Wiring is per equipment, so a tag belongs to every batch of its type
    run on that equipment; the batch's window picks out its own readings.
    """
    return any(
        wiring.tag_id == tag.id
        and session.get(
            EventFrameAttributeTemplate,
            wiring.event_frame_attribute_template_id,
        ).event_frame_template_id
        == frame.event_frame_template_id
        for wiring in services.list_event_frame_attributes(
            session, element_id=frame.element_id
        )
    )


def batch_measurements(session: Session, frame: EventFrame) -> list[dict]:
    """A batch's own measurements, each summed up over the batch's window.

    Only what the batch type defines: equipment measurements belong to the
    equipment. A measurement the equipment has no tag for yet (it has no
    area for its tags) is listed with ``tag_id`` None.
    """
    wired = {
        w.event_frame_attribute_template_id: w.tag_id
        for w in services.list_event_frame_attributes(
            session, element_id=frame.element_id
        )
    }
    out = []
    for template in services.list_event_frame_attribute_templates(
        session, frame.event_frame_template_id
    ):
        if template.is_property:
            continue  # what the batch is, shown with the batch itself
        tag_id = wired.get(template.id)
        row = {"name": template.name, "tag_id": tag_id, "count": 0}
        if tag_id is not None:
            tag = session.get(Tag, tag_id)
            row["unit"] = unit_of(tag)
            row["options"] = options_of(session, tag)
            zone = zone_for_tag(session, tag)
            bounds = {"start": frame.started_at, "end": frame.ended_at}

            def shown(found: list[TagValue]) -> dict | None:
                if not found:
                    return None
                return {
                    "value": value_text(session, tag, found[0]),
                    "at": local_time(from_utc(found[0].observed_at, zone)),
                }

            row["count"] = services.count_tag_values(
                session, tag_id, **bounds
            )
            row["first"] = shown(
                services.list_tag_values(
                    session, tag_id, **bounds, limit=1, oldest_first=True
                )
            )
            row["latest"] = shown(
                services.list_tag_values(session, tag_id, **bounds, limit=1)
            )
        out.append(row)
    return out


def property_text(value: object) -> str:
    """A property's value as a page shows it."""
    if isinstance(value, float):
        return number(value)
    return str(value)


def batch_properties(session: Session, frame: EventFrame) -> list[dict]:
    """Every property the batch's type defines, with its value or None."""
    held = services.event_frame_property_values(session, frame.id)
    return [
        {
            "name": template.name,
            "required": template.is_required,
            "text": (
                property_text(held[template.name])
                if template.name in held
                else None
            ),
        }
        for template in services.property_templates(
            session, frame.event_frame_template_id
        )
    ]


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
