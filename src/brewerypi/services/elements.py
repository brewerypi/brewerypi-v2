"""Service-layer CRUD for elements.

An element is an instance of an element template (FV01, FV02 of a Fermenter).
Its parent tree mirrors the template tree (rule "A1"): an instance of a
top-level template is top-level; an instance of a child template must have a
parent element that instances the template's parent template. Its
``tag_area`` (if set) must be in the template's site. ``element_template_id``
is fixed at creation.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brewerypi.models import Area, Element, ElementTemplate
from brewerypi.services._validation import (
    clean_name_segment,
    optional_str,
)
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

# Sentinel: distinguishes "leave unchanged" from "set to None" on update.
_UNSET = object()


def list_elements(
    session: Session,
    element_template_id: int | None = None,
    site_id: int | None = None,
    parent_id: int | None = None,
) -> list[Element]:
    """Return elements, optionally filtered by template, site, or parent."""
    stmt = select(Element).order_by(Element.name)
    if element_template_id is not None:
        stmt = stmt.where(Element.element_template_id == element_template_id)
    if parent_id is not None:
        stmt = stmt.where(Element.parent_id == parent_id)
    if site_id is not None:
        stmt = stmt.join(
            ElementTemplate,
            Element.element_template_id == ElementTemplate.id,
        ).where(ElementTemplate.site_id == site_id)
    return list(session.scalars(stmt).all())


def get_element(session: Session, element_id: int) -> Element:
    """Return one element, or raise NotFoundError."""
    element = session.get(Element, element_id)
    if element is None:
        raise NotFoundError(f"no element with id {element_id}")
    return element


def create_element(
    session: Session,
    element_template_id: int,
    name: str,
    description: str | None = None,
    tag_area_id: int | None = None,
    parent_id: int | None = None,
) -> Element:
    """Create an element instancing a template (see module docstring)."""
    name = clean_name_segment(name, "name", 45)
    template = session.get(ElementTemplate, element_template_id)
    if template is None:
        raise NotFoundError(
            f"no element template with id {element_template_id}"
        )
    _check_parent(session, template, parent_id)
    if tag_area_id is not None:
        _check_tag_area(session, tag_area_id, template.site_id)
    _check_unique(session, element_template_id, parent_id, name)
    element = Element(
        element_template_id=element_template_id,
        name=name,
        description=optional_str(description),
        tag_area_id=tag_area_id,
        parent_id=parent_id,
    )
    session.add(element)
    session.flush()
    # Wire the template's attributes (creates/adopts their tags). A no-op
    # when the element has no tag area yet; wiring then happens on assign.
    from brewerypi.services.element_attributes import wire_element

    wire_element(session, element)
    return element


def update_element(
    session: Session,
    element_id: int,
    name: str | None = None,
    description: str | None = None,
    tag_area_id: int | None = _UNSET,  # type: ignore[assignment]
    parent_id: int | None = _UNSET,  # type: ignore[assignment]
) -> Element:
    """Update an element. ``element_template_id`` is immutable.

    ``tag_area_id`` and ``parent_id`` accept an int (set), ``None`` (clear),
    or are omitted (unchanged). Re-parenting still obeys the A1 mirror rule.
    """
    element = get_element(session, element_id)
    template = element.element_template
    new_name = element.name
    new_parent = element.parent_id
    if name is not None:
        new_name = clean_name_segment(name, "name", 45)
    if parent_id is not _UNSET:
        _check_parent(session, template, parent_id)
        new_parent = parent_id
    if name is not None or parent_id is not _UNSET:
        _check_unique(
            session,
            element.element_template_id,
            new_parent,
            new_name,
            exclude_id=element_id,
        )
    renamed = new_name != element.name or new_parent != element.parent_id
    element.name = new_name
    element.parent_id = new_parent
    if description is not None:
        element.description = optional_str(description)
    gained_tag_area = False
    if tag_area_id is not _UNSET:
        if tag_area_id is not None:
            _check_tag_area(session, tag_area_id, template.site_id)
        gained_tag_area = (
            tag_area_id is not None and element.tag_area_id is None
        )
        element.tag_area_id = tag_area_id
    session.flush()
    from brewerypi.services.element_attributes import (
        resync_element_tag_names,
        wire_element,
    )

    if gained_tag_area:
        # Now that tags can be stored, wire the template's attributes.
        wire_element(session, element)
    if renamed:
        # The tag name embeds the element's path, so a rename or re-parent
        # must rename owned tags across the whole descendant subtree.
        resync_element_tag_names(session, element)
    return element


def delete_element(session: Session, element_id: int) -> None:
    """Delete an element, refusing if it has child elements.

    Its element attributes are unwired first: owned tags go with them (and the
    delete is refused if such a tag has readings), adopted tags are left.
    """
    element = get_element(session, element_id)
    children = session.scalar(
        select(func.count())
        .select_from(Element)
        .where(Element.parent_id == element_id)
    )
    if children:
        raise ValidationError(
            f"cannot delete element {element_id}: it has {children} child "
            "element(s); delete or reparent them first"
        )
    from brewerypi.services.element_attributes import (
        list_element_attributes,
        unwire_element_attribute,
    )

    for attribute in list_element_attributes(session, element_id=element_id):
        unwire_element_attribute(session, attribute.id)
    session.delete(element)
    session.flush()


def _check_parent(
    session: Session,
    template: ElementTemplate,
    parent_id: int | None,
) -> None:
    """Enforce the A1 mirror rule for a proposed parent element."""
    if template.parent_id is None:
        if parent_id is not None:
            raise ValidationError(
                "this element's template is top-level; the element must be "
                "top-level (no parent)"
            )
        return
    if parent_id is None:
        raise ValidationError(
            "this element's template has a parent template, so the element "
            f"needs a parent instancing template {template.parent_id}"
        )
    parent = session.get(Element, parent_id)
    if parent is None:
        raise NotFoundError(f"no element with id {parent_id}")
    if parent.element_template_id != template.parent_id:
        raise ValidationError(
            f"parent element {parent_id} must instance template "
            f"{template.parent_id} (this template's parent template)"
        )


def _check_tag_area(
    session: Session, tag_area_id: int, site_id: int
) -> None:
    """Ensure the tag area exists and is in the template's site."""
    area = session.get(Area, tag_area_id)
    if area is None:
        raise NotFoundError(f"no area with id {tag_area_id}")
    if area.site_id != site_id:
        raise ValidationError(
            f"tag area {tag_area_id} belongs to a different site"
        )


def _check_unique(
    session: Session,
    element_template_id: int,
    parent_id: int | None,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Names are unique within the parent (children) or template (roots)."""
    if parent_id is None:
        stmt = select(Element).where(
            Element.element_template_id == element_template_id,
            Element.parent_id.is_(None),
            Element.name == name,
        )
        scope = "its template"
    else:
        stmt = select(Element).where(
            Element.parent_id == parent_id,
            Element.name == name,
        )
        scope = "its parent element"
    if exclude_id is not None:
        stmt = stmt.where(Element.id != exclude_id)
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"an element named {name!r} already exists under {scope}"
        )
