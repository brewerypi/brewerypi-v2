"""Event frame attributes: wiring elements to the tags their frames write.

Wiring is scoped to the ELEMENT, not the frame: FV01 has one "Status" wiring,
and every fermentation run on FV01 writes its boundary values through that
same tag. Tags are named like element attribute tags -- the element's path
plus the attribute name (``Cellar.FV01.Status``) -- and use the same
find-or-create rule: create the tag (``owns_tag=True``) or adopt an existing
same-named one when its type is compatible (``owns_tag=False``); a type
conflict is an error. Adopted tags are routinely shared with the element's own
attributes, which is why orphan cleanup consults ``tag_is_referenced``.

Each function takes an open Session and raises the service exceptions on rule
violations. Callers own the transaction; these functions ``flush`` but never
commit.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi.models import (
    Element,
    EventFrameAttribute,
    EventFrameAttributeTemplate,
    EventFrameTemplate,
    Tag,
)
from brewerypi.services.element_attributes import (
    _remove_tag_if_disposable,
    build_tag_name,
)
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_event_frame_attributes(
    session: Session,
    element_id: int | None = None,
    event_frame_attribute_template_id: int | None = None,
) -> list[EventFrameAttribute]:
    """Return event frame attribute wirings, optionally filtered."""
    stmt = select(EventFrameAttribute)
    if element_id is not None:
        stmt = stmt.where(EventFrameAttribute.element_id == element_id)
    if event_frame_attribute_template_id is not None:
        stmt = stmt.where(
            EventFrameAttribute.event_frame_attribute_template_id
            == event_frame_attribute_template_id
        )
    return list(session.scalars(stmt).all())


def get_event_frame_attribute(
    session: Session, event_frame_attribute_id: int
) -> EventFrameAttribute:
    """Return one wiring, or raise NotFoundError."""
    attribute = session.get(EventFrameAttribute, event_frame_attribute_id)
    if attribute is None:
        raise NotFoundError(
            f"no event frame attribute with id {event_frame_attribute_id}"
        )
    return attribute


def wire_event_frame_attribute(
    session: Session,
    element: Element,
    attribute_template: EventFrameAttributeTemplate,
    tag_id: int | None = None,
) -> EventFrameAttribute:
    """Wire one event frame attribute template onto one element.

    With ``tag_id`` the given tag is linked (``owns_tag=False``); otherwise the
    tag is found-or-created by generated name in the element's tag area.
    """
    _check_not_already_wired(session, element.id, attribute_template.id)
    if tag_id is not None:
        tag = session.get(Tag, tag_id)
        if tag is None:
            raise NotFoundError(f"no tag with id {tag_id}")
        _check_type_compatible(tag, attribute_template)
        owns = False
    else:
        if element.tag_area_id is None:
            raise ValidationError(
                f"element {element.id} has no tag area, so its tags cannot "
                "be created; assign a tag area first"
            )
        tag, owns = _find_or_create_tag(
            session, element, attribute_template
        )
    attribute = EventFrameAttribute(
        element_id=element.id,
        event_frame_attribute_template_id=attribute_template.id,
        tag_id=tag.id,
        owns_tag=owns,
    )
    session.add(attribute)
    session.flush()
    return attribute


def wire_element_event_frame_attributes(
    session: Session, element: Element
) -> list[EventFrameAttribute]:
    """Wire every event frame attribute template that applies to an element.

    Those are the attribute templates of every event frame template defined on
    the element's element template. A no-op without a tag area; already-wired
    templates are skipped.
    """
    if element.tag_area_id is None:
        return []
    templates = session.scalars(
        select(EventFrameAttributeTemplate)
        .join(
            EventFrameTemplate,
            EventFrameAttributeTemplate.event_frame_template_id
            == EventFrameTemplate.id,
        )
        .where(
            EventFrameTemplate.element_template_id
            == element.element_template_id
        )
    ).all()
    wired: list[EventFrameAttribute] = []
    for template in templates:
        if _already_wired(session, element.id, template.id):
            continue
        wired.append(
            wire_event_frame_attribute(session, element, template)
        )
    return wired


def wire_event_frame_attribute_template(
    session: Session, attribute_template: EventFrameAttributeTemplate
) -> list[EventFrameAttribute]:
    """Wire a (new) event frame attribute template onto existing elements.

    Applies to every element instancing the element template that owns the
    attribute's event frame template. Elements without a tag area are skipped.
    """
    event_frame_template = session.get(
        EventFrameTemplate, attribute_template.event_frame_template_id
    )
    if event_frame_template is None:
        return []
    elements = session.scalars(
        select(Element).where(
            Element.element_template_id
            == event_frame_template.element_template_id
        )
    ).all()
    wired: list[EventFrameAttribute] = []
    for element in elements:
        if element.tag_area_id is None:
            continue
        if _already_wired(session, element.id, attribute_template.id):
            continue
        wired.append(
            wire_event_frame_attribute(
                session, element, attribute_template
            )
        )
    return wired


def unwire_event_frame_attribute(
    session: Session, event_frame_attribute_id: int
) -> None:
    """Remove a wiring, tidying up a tag we own.

    Always succeeds. An owned tag is deleted only when it is disposable --
    no readings and nothing else wired to it; otherwise it is left standing.
    """
    attribute = get_event_frame_attribute(
        session, event_frame_attribute_id
    )
    tag_id = attribute.tag_id
    owns = attribute.owns_tag
    session.delete(attribute)
    session.flush()
    if owns:
        _remove_tag_if_disposable(session, tag_id)


def _find_or_create_tag(
    session: Session,
    element: Element,
    attribute_template: EventFrameAttributeTemplate,
) -> tuple[Tag, bool]:
    """Find a same-named tag in the element's area, else create one."""
    name = build_tag_name(session, element, attribute_template)
    existing = session.scalars(
        select(Tag).where(
            Tag.area_id == element.tag_area_id, Tag.name == name
        )
    ).first()
    if existing is not None:
        _check_type_compatible(existing, attribute_template)
        return existing, False
    tag = Tag(
        area_id=element.tag_area_id,
        name=name,
        description=attribute_template.description,
        lookup_id=attribute_template.lookup_id,
        measurement_unit_id=attribute_template.measurement_unit_id,
    )
    session.add(tag)
    session.flush()
    return tag, True


def _check_type_compatible(
    tag: Tag, attribute_template: EventFrameAttributeTemplate
) -> None:
    """A tag may only back an attribute of the same type."""
    if (
        tag.lookup_id == attribute_template.lookup_id
        and tag.measurement_unit_id
        == attribute_template.measurement_unit_id
    ):
        return
    raise ValidationError(
        f"tag {tag.id} ({tag.name!r}) does not match event frame attribute "
        f"template {attribute_template.id} ({attribute_template.name!r}): "
        "the lookup / measurement unit must be the same"
    )


def _already_wired(
    session: Session, element_id: int, attribute_template_id: int
) -> bool:
    stmt = select(EventFrameAttribute).where(
        EventFrameAttribute.element_id == element_id,
        EventFrameAttribute.event_frame_attribute_template_id
        == attribute_template_id,
    )
    return session.scalars(stmt).first() is not None


def _check_not_already_wired(
    session: Session, element_id: int, attribute_template_id: int
) -> None:
    if _already_wired(session, element_id, attribute_template_id):
        raise ConflictError(
            f"element {element_id} already has event frame attribute "
            f"template {attribute_template_id}"
        )
