"""Service-layer CRUD for event frame templates.

An event frame template is a type of batch window (e.g. "Brew", "Fermentation")
defined for an element template. Templates nest, and the nesting mirrors the
element template tree (rule "A1"): a child event frame template's element
template must be a *direct child* of the parent's element template -- so a
"Brew" on a Brewhouse can parent a "Mashing" on the Brewhouse's Mash Mixer
child, and nothing else. A top-level event frame template (no parent) may sit
on any element template. ``element_template_id`` is fixed at creation.

Each function takes an open Session and raises the service exceptions on rule
violations. Callers own the transaction; these functions ``flush`` but never
commit.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brewerypi.models import ElementTemplate, EventFrameTemplate
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
    """Return event frame templates, optionally filtered."""
    stmt = select(EventFrameTemplate).order_by(EventFrameTemplate.name)
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
    description: str | None = None,
    parent_id: int | None = None,
) -> EventFrameTemplate:
    """Create an event frame template on an element template.

    With ``parent_id``, the A1 mirror applies: this template's element
    template must be a direct child of the parent template's element template.
    """
    name = clean_str(name, "name", 45)
    element_template = session.get(ElementTemplate, element_template_id)
    if element_template is None:
        raise NotFoundError(
            f"no element template with id {element_template_id}"
        )
    _check_parent(session, element_template, parent_id)
    _check_unique(session, element_template_id, name)
    template = EventFrameTemplate(
        element_template_id=element_template_id,
        name=name,
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
) -> EventFrameTemplate:
    """Update an event frame template; only provided fields change.

    ``element_template_id`` is immutable. ``parent_id`` re-parents (A1 still
    applies): an int nests it, ``None`` makes it top-level, omit to leave it.
    """
    template = get_event_frame_template(session, template_id)
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
    session.flush()
    return template


def delete_event_frame_template(
    session: Session, template_id: int
) -> None:
    """Delete an event frame template, refusing if it has child templates."""
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
