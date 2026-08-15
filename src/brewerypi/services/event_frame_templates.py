"""Service-layer CRUD for event frame templates.

An event frame template is a type of batch window (e.g. "Brew", "Fermentation")
defined for an element template. Templates nest, and the nesting mirrors the
element template tree (rule "A1"): a child event frame template's element
template must be a *direct child* of the parent's element template -- so a
"Brew" on a Brewhouse can parent a "Mashing" on the Brewhouse's Mash Mixer
child, and nothing else. A top-level event frame template (no parent) may sit
on any element template. ``element_template_id`` is fixed at creation.

Templates also carry a ``step_order``: the position of this step within its
parent process, unique among siblings. A top-level template is the process
rather than a step inside one, so its order is always 1 and callers need not
pass it; nesting under a parent makes the order a real choice, so it is
required there.

Each function takes an open Session and raises the service exceptions on rule
violations. Callers own the transaction; these functions ``flush`` but never
commit.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brewerypi.models import (
    ElementTemplate,
    EventFrame,
    EventFrameAttributeTemplate,
    EventFrameTemplate,
)
from brewerypi.services._validation import clean_str, optional_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

_UNSET = object()


def list_event_frame_templates(
    session: Session,
    element_template_id: int | None = None,
    parent_id: int | None = None,
) -> list[EventFrameTemplate]:
    """Return event frame templates in step order, optionally filtered."""
    stmt = select(EventFrameTemplate).order_by(
        EventFrameTemplate.step_order, EventFrameTemplate.name
    )
    if element_template_id is not None:
        stmt = stmt.where(
            EventFrameTemplate.element_template_id == element_template_id
        )
    if parent_id is not None:
        stmt = stmt.where(EventFrameTemplate.parent_id == parent_id)
    return list(session.scalars(stmt).all())


def get_event_frame_template(
    session: Session, template_id: int
) -> EventFrameTemplate:
    """Return one event frame template, or raise NotFoundError."""
    template = session.get(EventFrameTemplate, template_id)
    if template is None:
        raise NotFoundError(
            f"no event frame template with id {template_id}"
        )
    return template


def create_event_frame_template(
    session: Session,
    element_template_id: int,
    name: str,
    step_order: int | None = None,
    description: str | None = None,
    parent_id: int | None = None,
) -> EventFrameTemplate:
    """Create an event frame template on an element template.

    With ``parent_id``, the A1 mirror applies: this template's element
    template must be a direct child of the parent template's element template,
    and ``step_order`` is required -- it positions this step among its
    siblings. Without a parent the template is the process rather than a step
    inside one, so its order is always 1.
    """
    name = clean_str(name, "name", 45)
    element_template = session.get(ElementTemplate, element_template_id)
    if element_template is None:
        raise NotFoundError(
            f"no element template with id {element_template_id}"
        )
    _check_parent(session, element_template, parent_id)
    step_order = _resolve_step_order(step_order, parent_id)
    _check_unique(session, element_template_id, name)
    _check_step_order(session, parent_id, step_order)
    template = EventFrameTemplate(
        element_template_id=element_template_id,
        name=name,
        step_order=step_order,
        description=optional_str(description),
        parent_id=parent_id,
    )
    session.add(template)
    session.flush()
    return template


def update_event_frame_template(
    session: Session,
    template_id: int,
    name: str | None = None,
    description: str | None = None,
    parent_id: int | None = _UNSET,  # type: ignore[assignment]
    step_order: int | None = None,
) -> EventFrameTemplate:
    """Update an event frame template; only provided fields change.

    ``element_template_id`` is immutable. ``parent_id`` re-parents (A1 still
    applies): an int nests it, ``None`` makes it top-level, omit to leave it.
    Promoting a template to top-level resets ``step_order`` to 1, since that
    is the only order a process-level template can hold. Re-parenting keeps
    the current order unless ``step_order`` says otherwise, so a template
    moved into a parent whose slot is taken is refused -- pass the order it
    should occupy under its new parent.
    """
    template = get_event_frame_template(session, template_id)
    new_parent_id = template.parent_id if parent_id is _UNSET else parent_id
    if step_order is None:
        new_step_order = 1 if new_parent_id is None else template.step_order
    else:
        new_step_order = _resolve_step_order(step_order, new_parent_id)
    _check_step_order(
        session, new_parent_id, new_step_order, exclude_id=template_id
    )
    if name is not None:
        new_name = clean_str(name, "name", 45)
        _check_unique(
            session,
            template.element_template_id,
            new_name,
            exclude_id=template_id,
        )
        template.name = new_name
    if description is not None:
        template.description = optional_str(description)
    if parent_id is not _UNSET:
        if parent_id == template_id:
            raise ValidationError(
                "an event frame template can't be its own parent"
            )
        _check_parent(session, template.element_template, parent_id)
        template.parent_id = parent_id
    template.step_order = new_step_order
    session.flush()
    return template


def delete_event_frame_template(
    session: Session, template_id: int
) -> None:
    """Delete an event frame template.

    Refuses if it has child templates or any event frame instances (the
    latter previously surfaced as a raw foreign-key error). Its attribute
    templates are unwired from every element first, so an owned tag is
    cleaned up rather than orphaned.
    """
    template = get_event_frame_template(session, template_id)
    children = session.scalar(
        select(func.count())
        .select_from(EventFrameTemplate)
        .where(EventFrameTemplate.parent_id == template_id)
    )
    if children:
        raise ValidationError(
            f"cannot delete event frame template {template_id}: it has "
            f"{children} child template(s); delete them first"
        )
    instances = session.scalar(
        select(func.count())
        .select_from(EventFrame)
        .where(EventFrame.event_frame_template_id == template_id)
    )
    if instances:
        raise ValidationError(
            f"cannot delete event frame template {template_id}: it has "
            f"{instances} event frame(s); delete them first"
        )
    from brewerypi.services.event_frame_attributes import (
        list_event_frame_attributes,
        unwire_event_frame_attribute,
    )

    attribute_templates = session.scalars(
        select(EventFrameAttributeTemplate).where(
            EventFrameAttributeTemplate.event_frame_template_id
            == template_id
        )
    ).all()
    for attribute_template in attribute_templates:
        for wiring in list_event_frame_attributes(
            session,
            event_frame_attribute_template_id=attribute_template.id,
        ):
            unwire_event_frame_attribute(session, wiring.id)
    session.delete(template)
    session.flush()


def _check_parent(
    session: Session,
    element_template: ElementTemplate,
    parent_id: int | None,
) -> None:
    """Enforce the A1 mirror rule for a proposed parent template."""
    if parent_id is None:
        return
    parent = session.get(EventFrameTemplate, parent_id)
    if parent is None:
        raise NotFoundError(
            f"no event frame template with id {parent_id}"
        )
    if element_template.parent_id != parent.element_template_id:
        raise ValidationError(
            "A1 mirror: this template's element template "
            f"({element_template.id}) must be a direct child of the parent "
            f"template's element template ({parent.element_template_id})"
        )


def _resolve_step_order(
    step_order: int | None, parent_id: int | None
) -> int:
    """Validate a step order against the parent it will sit under."""
    if parent_id is None:
        if step_order is not None and step_order != 1:
            raise ValidationError(
                "a top-level event frame template is the process, not a step "
                f"inside one, so its step_order is always 1 (got {step_order})"
            )
        return 1
    if step_order is None:
        raise ValidationError(
            "step_order is required when nesting an event frame template: "
            "it positions this step among its siblings (Mashing 1, "
            "Lautering 2, Boil 3)"
        )
    if step_order < 1:
        raise ValidationError(
            f"step_order must be 1 or greater (got {step_order})"
        )
    return step_order


def _check_step_order(
    session: Session,
    parent_id: int | None,
    step_order: int,
    exclude_id: int | None = None,
) -> None:
    """Step orders are unique among siblings.

    Top-level templates are exempt: they all hold 1 by definition, so there
    is nothing to collide.
    """
    if parent_id is None:
        return
    stmt = select(EventFrameTemplate).where(
        EventFrameTemplate.parent_id == parent_id,
        EventFrameTemplate.step_order == step_order,
    )
    if exclude_id is not None:
        stmt = stmt.where(EventFrameTemplate.id != exclude_id)
    existing = session.scalars(stmt).first()
    if existing is not None:
        raise ConflictError(
            f"step {step_order} under event frame template {parent_id} is "
            f"already held by {existing.name!r}"
        )


def _check_unique(
    session: Session,
    element_template_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Names are unique within an element template."""
    stmt = select(EventFrameTemplate).where(
        EventFrameTemplate.element_template_id == element_template_id,
        EventFrameTemplate.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(EventFrameTemplate.id != exclude_id)
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"an event frame template named {name!r} already exists on "
            f"element template {element_template_id}"
        )
