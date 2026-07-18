"""Service-layer CRUD for event frame attribute templates.

Same type pattern as an element attribute template -- lookup-typed
(``lookup_id``), numeric (``measurement_unit_id``), or neither, mutually
exclusive, any lookup/unit belonging to the template's enterprise. In
addition, an event frame attribute template carries default start and end
values (written as readings at the frame's boundaries). Those defaults mirror
TagValue's storage: a numeric attribute uses the float defaults, a lookup-typed
attribute uses the lookup-value defaults (which must belong to ``lookup_id``
and be selectable).

Each function takes an open Session and raises the service exceptions on rule
violations. Callers own the transaction; these functions ``flush`` but never
commit.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi.models import (
    ElementTemplate,
    EventFrameAttributeTemplate,
    EventFrameTemplate,
    Lookup,
    LookupValue,
    MeasurementUnit,
    Site,
)
from brewerypi.services._validation import clean_name_segment, optional_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

# Sentinel: distinguishes "leave unchanged" from "set to None" on update.
_UNSET = object()


def list_event_frame_attribute_templates(
    session: Session, event_frame_template_id: int | None = None
) -> list[EventFrameAttributeTemplate]:
    """Return attribute templates, optionally filtered by event frame tmpl."""
    stmt = select(EventFrameAttributeTemplate).order_by(
        EventFrameAttributeTemplate.name
    )
    if event_frame_template_id is not None:
        stmt = stmt.where(
            EventFrameAttributeTemplate.event_frame_template_id
            == event_frame_template_id
        )
    return list(session.scalars(stmt).all())


def get_event_frame_attribute_template(
    session: Session, attribute_template_id: int
) -> EventFrameAttributeTemplate:
    """Return one attribute template, or raise NotFoundError."""
    template = session.get(
        EventFrameAttributeTemplate, attribute_template_id
    )
    if template is None:
        raise NotFoundError(
            "no event frame attribute template with id "
            f"{attribute_template_id}"
        )
    return template


def create_event_frame_attribute_template(
    session: Session,
    event_frame_template_id: int,
    name: str,
    description: str | None = None,
    lookup_id: int | None = None,
    measurement_unit_id: int | None = None,
    default_start_value: float | None = None,
    default_end_value: float | None = None,
    default_start_lookup_value_id: int | None = None,
    default_end_lookup_value_id: int | None = None,
) -> EventFrameAttributeTemplate:
    """Create an attribute template on an event frame template."""
    name = clean_name_segment(name, "name", 45)
    event_frame_template = session.get(
        EventFrameTemplate, event_frame_template_id
    )
    if event_frame_template is None:
        raise NotFoundError(
            f"no event frame template with id {event_frame_template_id}"
        )
    if lookup_id is not None and measurement_unit_id is not None:
        raise ValidationError(
            "an attribute template is either lookup-typed or numeric; "
            "provide lookup_id or measurement_unit_id, not both"
        )
    enterprise_id = _template_enterprise_id(session, event_frame_template)
    _check_lookup(session, lookup_id, enterprise_id)
    _check_measurement_unit(session, measurement_unit_id, enterprise_id)
    _check_unique(session, event_frame_template_id, name)
    _check_defaults(
        session,
        lookup_id,
        default_start_value,
        default_end_value,
        default_start_lookup_value_id,
        default_end_lookup_value_id,
    )
    template = EventFrameAttributeTemplate(
        event_frame_template_id=event_frame_template_id,
        name=name,
        description=optional_str(description),
        lookup_id=lookup_id,
        measurement_unit_id=measurement_unit_id,
        default_start_value=default_start_value,
        default_end_value=default_end_value,
        default_start_lookup_value_id=default_start_lookup_value_id,
        default_end_lookup_value_id=default_end_lookup_value_id,
    )
    session.add(template)
    session.flush()
    # Retroactively wire onto every element instancing the event frame
    # template's element template (elements without a tag area are skipped).
    from brewerypi.services.event_frame_attributes import (
        wire_event_frame_attribute_template,
    )

    wire_event_frame_attribute_template(session, template)
    return template


def update_event_frame_attribute_template(
    session: Session,
    attribute_template_id: int,
    name: str | None = None,
    description: str | None = None,
    default_start_value: float | None = _UNSET,  # type: ignore[assignment]
    default_end_value: float | None = _UNSET,  # type: ignore[assignment]
    default_start_lookup_value_id: int
    | None = _UNSET,  # type: ignore[assignment]
    default_end_lookup_value_id: int
    | None = _UNSET,  # type: ignore[assignment]
) -> EventFrameAttributeTemplate:
    """Update name/description and/or the default values.

    Changing the type (lookup vs numeric) is not supported here. Each default
    accepts a value (set), ``None`` (clear), or is omitted (unchanged); any
    default change re-validates the whole default set against the type.
    """
    template = get_event_frame_attribute_template(
        session, attribute_template_id
    )
    if name is not None:
        new_name = clean_name_segment(name, "name", 45)
        _check_unique(
            session,
            template.event_frame_template_id,
            new_name,
            exclude_id=attribute_template_id,
        )
        template.name = new_name
    if description is not None:
        template.description = optional_str(description)
    changed = (
        default_start_value,
        default_end_value,
        default_start_lookup_value_id,
        default_end_lookup_value_id,
    )
    if any(v is not _UNSET for v in changed):
        dsv = _pick(default_start_value, template.default_start_value)
        dev = _pick(default_end_value, template.default_end_value)
        dslv = _pick(
            default_start_lookup_value_id,
            template.default_start_lookup_value_id,
        )
        delv = _pick(
            default_end_lookup_value_id,
            template.default_end_lookup_value_id,
        )
        _check_defaults(session, template.lookup_id, dsv, dev, dslv, delv)
        template.default_start_value = dsv
        template.default_end_value = dev
        template.default_start_lookup_value_id = dslv
        template.default_end_lookup_value_id = delv
    session.flush()
    return template


def delete_event_frame_attribute_template(
    session: Session, attribute_template_id: int
) -> None:
    """Delete an attribute template."""
    template = get_event_frame_attribute_template(
        session, attribute_template_id
    )
    from brewerypi.services.event_frame_attributes import (
        list_event_frame_attributes,
        unwire_event_frame_attribute,
    )

    for attribute in list_event_frame_attributes(
        session, event_frame_attribute_template_id=attribute_template_id
    ):
        unwire_event_frame_attribute(session, attribute.id)
    session.delete(template)
    session.flush()


def _pick(provided, existing):
    return existing if provided is _UNSET else provided


def _template_enterprise_id(
    session: Session, event_frame_template: EventFrameTemplate
) -> int:
    element_template = session.get(
        ElementTemplate, event_frame_template.element_template_id
    )
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


def _check_defaults(
    session: Session,
    lookup_id: int | None,
    default_start_value: float | None,
    default_end_value: float | None,
    default_start_lookup_value_id: int | None,
    default_end_lookup_value_id: int | None,
) -> None:
    """Defaults must match the attribute's type (lookup vs numeric)."""
    if lookup_id is not None:
        if default_start_value is not None or default_end_value is not None:
            raise ValidationError(
                "a lookup-typed attribute's defaults must be lookup values, "
                "not numbers"
            )
        _check_default_lookup_value(
            session, default_start_lookup_value_id, lookup_id, "start"
        )
        _check_default_lookup_value(
            session, default_end_lookup_value_id, lookup_id, "end"
        )
    else:
        if (
            default_start_lookup_value_id is not None
            or default_end_lookup_value_id is not None
        ):
            raise ValidationError(
                "a numeric attribute's defaults must be numbers, not lookup "
                "values"
            )


def _check_default_lookup_value(
    session: Session,
    lookup_value_id: int | None,
    lookup_id: int,
    which: str,
) -> None:
    if lookup_value_id is None:
        return
    value = session.get(LookupValue, lookup_value_id)
    if value is None:
        raise NotFoundError(f"no lookup value with id {lookup_value_id}")
    if value.lookup_id != lookup_id:
        raise ValidationError(
            f"default {which} value {lookup_value_id} does not belong to "
            f"lookup {lookup_id}"
        )
    if not value.is_selectable:
        raise ValidationError(
            f"default {which} value {lookup_value_id} is not selectable"
        )


def _check_unique(
    session: Session,
    event_frame_template_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    stmt = select(EventFrameAttributeTemplate).where(
        EventFrameAttributeTemplate.event_frame_template_id
        == event_frame_template_id,
        EventFrameAttributeTemplate.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(EventFrameAttributeTemplate.id != exclude_id)
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"an attribute template named {name!r} already exists on "
            f"event frame template {event_frame_template_id}"
        )
