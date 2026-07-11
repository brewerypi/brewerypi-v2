"""Element attributes: the wiring between elements, attribute templates, tags.

An element attribute realizes one attribute template on one element and points
at the Tag that stores its data. Tags are named by the element's path plus the
attribute name, e.g. ``Cellar.FV01.Temperature`` (see ``build_tag_name``).

Wiring is find-or-create: if no tag with the computed name exists in the
element's tag area, one is created (typed from the attribute template,
``owns_tag=True``); if a tag with that name already exists and its type is
compatible, it is adopted (``owns_tag=False``); if a tag exists but its type
conflicts, that is an error. Adopted tags may be shared by several attributes.

Each function takes an open Session and raises the service exceptions on rule
violations. Callers own the transaction; these functions ``flush`` but never
commit.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brewerypi.models import (
    Element,
    ElementAttribute,
    ElementAttributeTemplate,
    Tag,
    TagValue,
)
from brewerypi.services._validation import TAG_PATH_SEPARATOR
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def build_tag_name(
    session: Session,
    element: Element,
    attribute_template: ElementAttributeTemplate,
) -> str:
    """Return the generated tag name for an element + attribute template.

    The element's ancestry, root first, then the attribute name, joined by the
    tag path separator: ``Cellar.FV01.Temperature``.
    """
    segments: list[str] = []
    current: Element | None = element
    seen: set[int] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        segments.append(current.name)
        current = (
            session.get(Element, current.parent_id)
            if current.parent_id is not None
            else None
        )
    segments.reverse()
    segments.append(attribute_template.name)
    name = TAG_PATH_SEPARATOR.join(segments)
    if len(name) > 255:
        raise ValidationError(
            f"generated tag name {name!r} exceeds 255 characters; "
            "shorten the element or attribute names"
        )
    return name


def list_element_attributes(
    session: Session,
    element_id: int | None = None,
    element_attribute_template_id: int | None = None,
) -> list[ElementAttribute]:
    """Return element attributes, optionally filtered."""
    stmt = select(ElementAttribute)
    if element_id is not None:
        stmt = stmt.where(ElementAttribute.element_id == element_id)
    if element_attribute_template_id is not None:
        stmt = stmt.where(
            ElementAttribute.element_attribute_template_id
            == element_attribute_template_id
        )
    return list(session.scalars(stmt).all())


def get_element_attribute(
    session: Session, element_attribute_id: int
) -> ElementAttribute:
    """Return one element attribute, or raise NotFoundError."""
    attribute = session.get(ElementAttribute, element_attribute_id)
    if attribute is None:
        raise NotFoundError(
            f"no element attribute with id {element_attribute_id}"
        )
    return attribute


def wire_element_attribute(
    session: Session,
    element: Element,
    attribute_template: ElementAttributeTemplate,
    tag_id: int | None = None,
) -> ElementAttribute:
    """Wire one attribute template onto one element.

    With ``tag_id`` the given tag is linked (``owns_tag=False``). Otherwise the
    tag is found-or-created by generated name in the element's tag area.
    Requires the element to have a tag area unless an explicit tag is given.
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
    attribute = ElementAttribute(
        element_id=element.id,
        element_attribute_template_id=attribute_template.id,
        tag_id=tag.id,
        owns_tag=owns,
    )
    session.add(attribute)
    session.flush()
    return attribute


def wire_element(session: Session, element: Element) -> list[ElementAttribute]:
    """Wire every attribute template of an element's template onto it.

    Skips templates already wired. A no-op if the element has no tag area.
    """
    if element.tag_area_id is None:
        return []
    templates = session.scalars(
        select(ElementAttributeTemplate).where(
            ElementAttributeTemplate.element_template_id
            == element.element_template_id
        )
    ).all()
    wired: list[ElementAttribute] = []
    for template in templates:
        if _already_wired(session, element.id, template.id):
            continue
        wired.append(
            wire_element_attribute(session, element, template)
        )
    return wired


def wire_attribute_template(
    session: Session, attribute_template: ElementAttributeTemplate
) -> list[ElementAttribute]:
    """Wire a (new) attribute template onto every existing instance.

    Elements without a tag area are skipped; they get wired when one is
    assigned.
    """
    elements = session.scalars(
        select(Element).where(
            Element.element_template_id
            == attribute_template.element_template_id
        )
    ).all()
    wired: list[ElementAttribute] = []
    for element in elements:
        if element.tag_area_id is None:
            continue
        if _already_wired(session, element.id, attribute_template.id):
            continue
        wired.append(
            wire_element_attribute(session, element, attribute_template)
        )
    return wired


def resync_element_tag_names(
    session: Session, element: Element
) -> list[Tag]:
    """Rename owned tags to match the element's (renamed/moved) path.

    Applies to the element and its whole descendant subtree, since the tag
    name embeds the ancestry. Adopted tags (``owns_tag=False``) are left
    alone -- their names are not ours to change.
    """
    renamed: list[Tag] = []
    for descendant in _subtree(session, element):
        attributes = session.scalars(
            select(ElementAttribute).where(
                ElementAttribute.element_id == descendant.id
            )
        ).all()
        for attribute in attributes:
            if not attribute.owns_tag:
                continue
            tag = session.get(Tag, attribute.tag_id)
            template = session.get(
                ElementAttributeTemplate,
                attribute.element_attribute_template_id,
            )
            new_name = build_tag_name(session, descendant, template)
            if tag.name == new_name:
                continue
            _check_tag_name_free(
                session, tag.area_id, new_name, exclude_id=tag.id
            )
            tag.name = new_name
            renamed.append(tag)
    session.flush()
    return renamed


def unwire_element_attribute(
    session: Session, element_attribute_id: int
) -> None:
    """Remove an element attribute, and its tag if we own it.

    Refuses when an owned tag has recorded readings (deleting it would
    destroy history). An adopted tag is left in place; only the link goes.
    """
    attribute = get_element_attribute(session, element_attribute_id)
    tag_id = attribute.tag_id
    owns = attribute.owns_tag
    if owns:
        readings = session.scalar(
            select(func.count())
            .select_from(TagValue)
            .where(TagValue.tag_id == tag_id)
        )
        if readings:
            raise ValidationError(
                f"cannot remove element attribute {element_attribute_id}: "
                f"its tag has {readings} recorded reading(s); deleting "
                "would destroy that history"
            )
    session.delete(attribute)
    session.flush()
    if owns:
        tag = session.get(Tag, tag_id)
        if tag is not None and not tag.element_attributes:
            session.delete(tag)
            session.flush()


def _subtree(session: Session, element: Element) -> list[Element]:
    """Return the element and all its descendants."""
    found = [element]
    frontier = [element.id]
    while frontier:
        children = session.scalars(
            select(Element).where(Element.parent_id.in_(frontier))
        ).all()
        if not children:
            break
        found.extend(children)
        frontier = [child.id for child in children]
    return found


def _find_or_create_tag(
    session: Session,
    element: Element,
    attribute_template: ElementAttributeTemplate,
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
    tag: Tag, attribute_template: ElementAttributeTemplate
) -> None:
    """A tag may only back an attribute of the same type."""
    if (
        tag.lookup_id == attribute_template.lookup_id
        and tag.measurement_unit_id
        == attribute_template.measurement_unit_id
    ):
        return
    raise ValidationError(
        f"tag {tag.id} ({tag.name!r}) does not match attribute template "
        f"{attribute_template.id} ({attribute_template.name!r}): the "
        "lookup / measurement unit must be the same"
    )


def _already_wired(
    session: Session, element_id: int, attribute_template_id: int
) -> bool:
    stmt = select(ElementAttribute).where(
        ElementAttribute.element_id == element_id,
        ElementAttribute.element_attribute_template_id
        == attribute_template_id,
    )
    return session.scalars(stmt).first() is not None


def _check_not_already_wired(
    session: Session, element_id: int, attribute_template_id: int
) -> None:
    if _already_wired(session, element_id, attribute_template_id):
        raise ConflictError(
            f"element {element_id} already has attribute template "
            f"{attribute_template_id}"
        )


def _check_tag_name_free(
    session: Session, area_id: int, name: str, exclude_id: int
) -> None:
    stmt = select(Tag).where(
        Tag.area_id == area_id, Tag.name == name, Tag.id != exclude_id
    )
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"a tag named {name!r} already exists in area {area_id}; "
            "rename would collide"
        )
