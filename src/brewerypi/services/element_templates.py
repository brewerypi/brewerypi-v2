"""Service-layer CRUD for element templates.

Element templates form a site-scoped, self-referential tree: a top-level
template has no parent. Each function takes an open Session and raises the
service exceptions on rule violations. Callers own the transaction; these
functions ``flush`` but never commit.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brewerypi.models import ElementTemplate, Site
from brewerypi.services._validation import clean_str, optional_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

# Sentinel so ``update`` can tell "leave the parent unchanged" from
# "set the parent to None" (i.e. make the template top-level).
_UNSET = object()


def list_element_templates(
    session: Session, site_id: int | None = None
) -> list[ElementTemplate]:
    """Return element templates, optionally filtered by site."""
    stmt = select(ElementTemplate).order_by(ElementTemplate.name)
    if site_id is not None:
        stmt = stmt.where(ElementTemplate.site_id == site_id)
    return list(session.scalars(stmt).all())


def get_element_template(
    session: Session, template_id: int
) -> ElementTemplate:
    """Return one element template, or raise NotFoundError."""
    template = session.get(ElementTemplate, template_id)
    if template is None:
        raise NotFoundError(f"no element template with id {template_id}")
    return template


def create_element_template(
    session: Session,
    site_id: int,
    name: str,
    description: str | None = None,
    parent_id: int | None = None,
) -> ElementTemplate:
    """Create an element template under a site.

    Validates the site exists, the name is unique within the site, and (if a
    parent is given) that the parent exists and belongs to the same site.
    """
    name = clean_str(name, "name", 45)
    if session.get(Site, site_id) is None:
        raise NotFoundError(f"no site with id {site_id}")
    if parent_id is not None:
        _check_parent(session, parent_id, site_id)
    _check_unique(session, site_id, name)
    template = ElementTemplate(
        site_id=site_id,
        name=name,
        description=optional_str(description),
        parent_id=parent_id,
    )
    session.add(template)
    session.flush()
    return template


def update_element_template(
    session: Session,
    template_id: int,
    name: str | None = None,
    description: str | None = None,
    parent_id: int | None = _UNSET,  # type: ignore[assignment]
) -> ElementTemplate:
    """Update an element template; only provided fields change.

    Pass ``parent_id`` to re-parent: an int moves the template under that
    parent (same site, no cycles), ``None`` makes it top-level. Omit it to
    leave the parent unchanged.
    """
    template = get_element_template(session, template_id)
    if name is not None:
        new_name = clean_str(name, "name", 45)
        _check_unique(
            session, template.site_id, new_name, exclude_id=template_id
        )
        template.name = new_name
    if description is not None:
        template.description = optional_str(description)
    if parent_id is not _UNSET:
        if parent_id is not None:
            _check_parent(session, parent_id, template.site_id)
            _check_no_cycle(session, template_id, parent_id)
        template.parent_id = parent_id
    session.flush()
    return template


def delete_element_template(session: Session, template_id: int) -> None:
    """Delete an element template, refusing if it has child templates."""
    template = get_element_template(session, template_id)
    children = session.scalar(
        select(func.count())
        .select_from(ElementTemplate)
        .where(ElementTemplate.parent_id == template_id)
    )
    if children:
        raise ValidationError(
            f"cannot delete element template {template_id}: it has "
            f"{children} child template(s); delete or reparent them first"
        )
    session.delete(template)
    session.flush()


def _check_parent(
    session: Session, parent_id: int, site_id: int
) -> None:
    """Ensure a proposed parent exists and is in the same site."""
    parent = session.get(ElementTemplate, parent_id)
    if parent is None:
        raise NotFoundError(f"no element template with id {parent_id}")
    if parent.site_id != site_id:
        raise ValidationError(
            f"parent template {parent_id} belongs to a different site"
        )


def _check_no_cycle(
    session: Session, template_id: int, new_parent_id: int
) -> None:
    """Refuse a re-parent that would make a template its own ancestor."""
    seen: set[int] = set()
    current: int | None = new_parent_id
    while current is not None:
        if current == template_id:
            raise ValidationError(
                "a template cannot be its own parent or ancestor"
            )
        if current in seen:
            break
        seen.add(current)
        parent = session.get(ElementTemplate, current)
        current = parent.parent_id if parent is not None else None


def _check_unique(
    session: Session,
    site_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Raise ConflictError if the name is taken within the site."""
    stmt = select(ElementTemplate).where(
        ElementTemplate.site_id == site_id,
        ElementTemplate.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(ElementTemplate.id != exclude_id)
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"an element template named {name!r} already exists in "
            f"site {site_id}"
        )
