"""How rows are presented to anything outside the service layer.

Each function turns a model row into the plain dict a consumer shows: names
resolved, times converted to local with the zone they are in, and authors
named. The MCP tools return these as they are, so a change here changes what
they report.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from brewerypi import services
from brewerypi.models import (
    Area,
    Element,
    ElementAttribute,
    ElementAttributeTemplate,
    ElementTemplate,
    Enterprise,
    EventFrame,
    EventFrameAttribute,
    EventFrameAttributeTemplate,
    EventFrameTemplate,
    Lookup,
    LookupValue,
    MeasurementUnit,
    Membership,
    Site,
    Tag,
    TagValue,
    User,
)
from brewerypi.timezones import (
    DEFAULT_TIMEZONE,
    from_utc,
    zone_for_frame,
    zone_for_tag,
)


def person_name(session: Session, user_id: int | None) -> str | None:
    """How to name the author of a row: their name, else their email."""
    user = session.get(User, user_id) if user_id is not None else None
    return (user.name or user.email) if user is not None else None


def reading_authors(session: Session, tv: TagValue) -> dict:
    return {
        "recorded_by": person_name(session, tv.recorded_by_id),
        "updated_by": person_name(session, tv.updated_by_id),
    }


def reading_dict(session: Session, tv: TagValue) -> dict:
    if tv.lookup_value_id is not None:
        lv = session.get(LookupValue, tv.lookup_value_id)
        value: object = lv.name if lv is not None else None
        vtype = "lookup"
    else:
        value = tv.value
        vtype = "numeric"
    tag = session.get(Tag, tv.tag_id)
    zone = zone_for_tag(session, tag) if tag is not None else DEFAULT_TIMEZONE
    return {
        "id": tv.id,
        "tag_id": tv.tag_id,
        "observed_at": from_utc(tv.observed_at, zone),
        "timezone": zone,
        "value": value,
        "type": vtype,
        **reading_authors(session, tv),
    }


def element_dict(e: Element) -> dict:
    return {
        "id": e.id,
        "element_template_id": e.element_template_id,
        "tag_area_id": e.tag_area_id,
        "parent_id": e.parent_id,
        "name": e.name,
        "description": e.description,
    }


def element_attribute_dict(
    session: Session, ea: ElementAttribute
) -> dict:
    """Attribute rows carry the template's name and the wired tag's name.

    An element attribute has no name of its own -- it inherits the attribute
    template's -- so both are resolved here for a usable view.
    """
    template = session.get(
        ElementAttributeTemplate, ea.element_attribute_template_id
    )
    tag = session.get(Tag, ea.tag_id)
    return {
        "id": ea.id,
        "element_id": ea.element_id,
        "element_attribute_template_id": ea.element_attribute_template_id,
        "name": template.name if template is not None else None,
        "tag_id": ea.tag_id,
        "tag_name": tag.name if tag is not None else None,
        "owns_tag": ea.owns_tag,
    }


def event_frame_template_dict(t: EventFrameTemplate) -> dict:
    return {
        "id": t.id,
        "element_template_id": t.element_template_id,
        "parent_id": t.parent_id,
        "step_order": t.step_order,
        "parent_flow": t.parent_flow,
        "name": t.name,
        "description": t.description,
    }


def event_frame_dict(session: Session, f: EventFrame) -> dict:
    zone = zone_for_frame(session, f)
    element = session.get(Element, f.element_id)
    return {
        "id": f.id,
        "element_id": f.element_id,
        "element_name": element.name if element is not None else None,
        "element_template_id": (
            element.element_template_id if element is not None else None
        ),
        "event_frame_template_id": f.event_frame_template_id,
        "parent_id": f.parent_id,
        "source_event_frame_id": f.source_event_frame_id,
        "destination_event_frame_id": f.destination_event_frame_id,
        "name": f.name,
        "started_at": from_utc(f.started_at, zone),
        "ended_at": (
            from_utc(f.ended_at, zone) if f.ended_at is not None else None
        ),
        "open": f.ended_at is None,
        "timezone": zone,
        "started_by": person_name(session, f.started_by_id),
        "ended_by": person_name(session, f.ended_by_id),
        "updated_by": person_name(session, f.updated_by_id),
    }


def event_frame_attribute_dict(
    session: Session, a: EventFrameAttribute
) -> dict:
    template = session.get(
        EventFrameAttributeTemplate,
        a.event_frame_attribute_template_id,
    )
    tag = session.get(Tag, a.tag_id)
    return {
        "id": a.id,
        "element_id": a.element_id,
        "event_frame_attribute_template_id": (
            a.event_frame_attribute_template_id
        ),
        "name": template.name if template is not None else None,
        "tag_id": a.tag_id,
        "tag_name": tag.name if tag is not None else None,
        "owns_tag": a.owns_tag,
    }


def unit_dict(unit: MeasurementUnit) -> dict:
    return {
        "id": unit.id,
        "enterprise_id": unit.enterprise_id,
        "abbreviation": unit.abbreviation,
        "name": unit.name,
        "description": unit.description,
    }


def lookup_dict(lookup: Lookup) -> dict:
    return {
        "id": lookup.id,
        "enterprise_id": lookup.enterprise_id,
        "name": lookup.name,
    }


def lookup_value_dict(value: LookupValue) -> dict:
    return {
        "id": value.id,
        "lookup_id": value.lookup_id,
        "name": value.name,
        "is_selectable": value.is_selectable,
    }


def tag_dict(tag: Tag) -> dict:
    return {
        "id": tag.id,
        "area_id": tag.area_id,
        "name": tag.name,
        "description": tag.description,
        "lookup_id": tag.lookup_id,
        "measurement_unit_id": tag.measurement_unit_id,
    }


def area_dict(area: Area) -> dict:
    return {
        "id": area.id,
        "site_id": area.site_id,
        "abbreviation": area.abbreviation,
        "name": area.name,
        "description": area.description,
    }


def site_dict(site: Site) -> dict:
    return {
        "id": site.id,
        "enterprise_id": site.enterprise_id,
        "abbreviation": site.abbreviation,
        "name": site.name,
        "description": site.description,
        "timezone": site.timezone,
    }


def enterprise_dict(ent: Enterprise) -> dict:
    return {
        "id": ent.id,
        "abbreviation": ent.abbreviation,
        "name": ent.name,
        "description": ent.description,
    }


def element_template_dict(t: ElementTemplate) -> dict:
    return {
        "id": t.id,
        "site_id": t.site_id,
        "parent_id": t.parent_id,
        "name": t.name,
        "description": t.description,
        "exclusive": t.exclusive,
    }


def attribute_template_dict(t: ElementAttributeTemplate) -> dict:
    return {
        "id": t.id,
        "element_template_id": t.element_template_id,
        "lookup_id": t.lookup_id,
        "measurement_unit_id": t.measurement_unit_id,
        "name": t.name,
        "description": t.description,
    }


def event_frame_attribute_template_dict(
    t: EventFrameAttributeTemplate,
) -> dict:
    return {
        "id": t.id,
        "event_frame_template_id": t.event_frame_template_id,
        "lookup_id": t.lookup_id,
        "measurement_unit_id": t.measurement_unit_id,
        "name": t.name,
        "description": t.description,
        "default_start_value": t.default_start_value,
        "default_end_value": t.default_end_value,
        "default_start_lookup_value_id": t.default_start_lookup_value_id,
        "default_end_lookup_value_id": t.default_end_lookup_value_id,
    }


def membership_dict(m: Membership) -> dict:
    return {
        "id": m.id,
        "user_id": m.user_id,
        "email": m.user.email,
        "enterprise_id": m.enterprise_id,
        "role": m.role,
        "site_ids": services.membership_site_ids(m),
    }


def user_dict(user: User, scope: set[int] | None) -> dict:
    """A user, with only the memberships the caller manages."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "timezone": user.timezone,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
        "signed_in": user.auth_subject is not None,
        "memberships": [
            membership_dict(m)
            for m in user.memberships
            if scope is None or m.enterprise_id in scope
        ],
    }
