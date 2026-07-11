"""Service-layer CRUD for element attribute templates.

An attribute template defines an attribute on an element template (e.g.
Temperature on a Fermenter). Like a tag it is either lookup-typed
(``lookup_id``) or numeric (``measurement_unit_id``) or neither -- never both
-- and any referenced lookup or unit must belong to the template's
enterprise.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi.models import (
    ElementAttributeTemplate,
    ElementTemplate,
    Lookup,
    MeasurementUnit,
    Site,
)
from brewerypi.services._validation import (
    clean_name_segment,
    optional_str,
)
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_element_attribute_templates(
    session: Session, element_template_id: int | None = None
) -> list[ElementAttributeTemplate]:
    """Return attribute templates, optionally filtered by element template."""
    stmt = select(ElementAttributeTemplate).order_by(
        ElementAttributeTemplate.name
    )
    if element_template_id is not None:
        stmt = stmt.where(
            ElementAttributeTemplate.element_template_id
            == element_template_id
        )
    return list(session.scalars(stmt).all())


def get_element_attribute_template(
    session: Session, attribute_template_id: int
) -> ElementAttributeTemplate:
    """Return one attribute template, or raise NotFoundError."""
    template = session.get(
        ElementAttributeTemplate, attribute_template_id
    )
    if template is None:
        raise NotFoundError(
            f"no element attribute template with id {attribute_template_id}"
        )
    return template


def create_element_attribute_template(
    session: Session,
    element_template_id: int,
    name: str,
    description: str | None = None,
    lookup_id: int | None = None,
    measurement_unit_id: int | None = None,
) -> ElementAttributeTemplate:
    """Create an attribute template on an element template.

    Validates the element template exists, the name is unique within it, that
    the attribute is not both lookup-typed and numeric, and that any lookup or
    unit belongs to the element template's enterprise.
    """
    name = clean_name_segment(name, "name", 45)
    element_template = session.get(ElementTemplate, element_template_id)
    if element_template is None:
        raise NotFoundError(
            f"no element template with id {element_template_id}"
        )
    if lookup_id is not None and measurement_unit_id is not None:
        raise ValidationError(
            "an attribute template is either lookup-typed or numeric; "
            "provide lookup_id or measurement_unit_id, not both"
        )
    enterprise_id = _template_enterprise_id(session, element_template)
    _check_lookup(session, lookup_id, enterprise_id)
    _check_measurement_unit(session, measurement_unit_id, enterprise_id)
    _check_unique(session, element_template_id, name)
    template = ElementAttributeTemplate(
        element_template_id=element_template_id,
        name=name,
        description=optional_str(description),
        lookup_id=lookup_id,
        measurement_unit_id=measurement_unit_id,
    )
    session.add(template)
    session.flush()
    # Retroactively wire this attribute onto every existing instance of the
    # element template (elements without a tag area are skipped).
    from brewerypi.services.element_attributes import wire_attribute_template

    wire_attribute_template(session, template)
    return template


def update_element_attribute_template(
    session: Session,
    attribute_template_id: int,
    name: str | None = None,
    description: str | None = None,
) -> ElementAttributeTemplate:
    """Update an attribute template's name and/or description.

    Changing the type (lookup vs numeric) is not supported here; delete and
    recreate an unused attribute template instead.
    """
    template = get_element_attribute_template(
        session, attribute_template_id
    )
    if name is not None:
        new_name = clean_name_segment(name, "name", 45)
        _check_unique(
            session,
            template.element_template_id,
            new_name,
            exclude_id=attribute_template_id,
        )
        template.name = new_name
    if description is not None:
        template.description = optional_str(description)
    session.flush()
    return template


def delete_element_attribute_template(
    session: Session, attribute_template_id: int
) -> None:
    """Delete an attribute template, unwiring it from every element first.

    Owned tags go with their attributes (refused if such a tag has readings);
    adopted tags are left in place.
    """
    template = get_element_attribute_template(
        session, attribute_template_id
    )
    from brewerypi.services.element_attributes import (
        list_element_attributes,
        unwire_element_attribute,
    )

    for attribute in list_element_attributes(
        session, element_attribute_template_id=attribute_template_id
    ):
        unwire_element_attribute(session, attribute.id)
    session.delete(template)
    session.flush()


def _template_enterprise_id(
    session: Session, element_template: ElementTemplate
) -> int:
    site = session.get(Site, element_template.site_id)
    return site.enterprise_id


def _check_lookup(
    session: Session, lookup_id: int | None, enterprise_id: int
) -> None:
    if lookup_id is None:
        return
    lookup = session.get(Lookup, lookup_id)
    if lookup is None:
        raise NotFoundError(f"no lookup with id {lookup_id}")
    if lookup.enterprise_id != enterprise_id:
        raise ValidationError(
            f"lookup {lookup_id} belongs to a different enterprise"
        )


def _check_measurement_unit(
    session: Session, unit_id: int | None, enterprise_id: int
) -> None:
    if unit_id is None:
        return
    unit = session.get(MeasurementUnit, unit_id)
    if unit is None:
        raise NotFoundError(f"no measurement unit with id {unit_id}")
    if unit.enterprise_id != enterprise_id:
        raise ValidationError(
            f"measurement unit {unit_id} belongs to a different enterprise"
        )


def _check_unique(
    session: Session,
    element_template_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Raise ConflictError if the name is taken within the element template."""
    stmt = select(ElementAttributeTemplate).where(
        ElementAttributeTemplate.element_template_id == element_template_id,
        ElementAttributeTemplate.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(ElementAttributeTemplate.id != exclude_id)
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"an attribute template named {name!r} already exists on "
            f"element template {element_template_id}"
        )
