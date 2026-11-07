"""Batch properties: what a batch is, such as its brand.

A property is an event frame attribute template with ``is_property`` set.
Unlike a measurement, which is wired per ELEMENT to a tag and read from the
batch's time window, a property's value belongs to the batch itself: one row
per batch and property, so two brews running side by side on one brewhouse
never share it. Values are given the way readings are: a list property takes
the name of a selectable option on its list, a numeric one a number.

A property is set when its batch starts. Left blank, it takes the start
default, then the value its source batch holds on the same list (a canning
run takes its brite tank's brand), and a required one with neither refuses
the start. Afterwards it can be changed; only the latest value and who last
changed it are kept.

Each function takes an open Session and raises the service exceptions on rule
violations. Callers own the transaction; these functions ``flush`` but never
commit.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brewerypi.models import (
    EventFrame,
    EventFrameAttributeTemplate,
    EventFrameProperty,
    EventFrameTemplate,
    LookupValue,
)
from brewerypi.services.exceptions import NotFoundError, ValidationError
from brewerypi.services.tag_values import _resolve_lookup_value

#: What a caller passes for one property: an option's name, a number, or
#: nothing (blank).
PropertyInput = str | float | int | None


def property_templates(
    session: Session, event_frame_template_id: int
) -> list[EventFrameAttributeTemplate]:
    """The properties a batch type defines, by name."""
    return list(
        session.scalars(
            select(EventFrameAttributeTemplate)
            .where(
                EventFrameAttributeTemplate.event_frame_template_id
                == event_frame_template_id,
                EventFrameAttributeTemplate.is_property.is_(True),
            )
            .order_by(EventFrameAttributeTemplate.name)
        ).all()
    )


def list_event_frame_properties(
    session: Session, event_frame_id: int
) -> list[EventFrameProperty]:
    """A batch's property values."""
    return list(
        session.scalars(
            select(EventFrameProperty).where(
                EventFrameProperty.event_frame_id == event_frame_id
            )
        ).all()
    )


def property_value(session: Session, row: EventFrameProperty) -> object:
    """A stored value as a caller shows it: the option's name, or a number."""
    if row.lookup_value_id is not None:
        option = session.get(LookupValue, row.lookup_value_id)
        return option.name if option is not None else None
    return row.value


def event_frame_property_values(
    session: Session, event_frame_id: int
) -> dict[str, object]:
    """A batch's properties as ``{name: value}``."""
    out = {}
    for row in list_event_frame_properties(session, event_frame_id):
        template = session.get(
            EventFrameAttributeTemplate, row.event_frame_attribute_template_id
        )
        out[template.name] = property_value(session, row)
    return dict(sorted(out.items()))


def apply_start_properties(
    session: Session,
    frame: EventFrame,
    given: Mapping[str, PropertyInput] | None,
    recorded_by_id: int | None,
) -> list[EventFrameProperty]:
    """Set a starting batch's properties, refusing a missing required one.

    ``given`` maps property names to values. A blank one takes the start
    default, then the source batch's value on the same list.
    """
    templates = property_templates(session, frame.event_frame_template_id)
    given = dict(given or {})
    _check_names(session, frame, templates, given)
    written = []
    for template in templates:
        raw = given.get(template.name)
        if _is_blank(raw):
            stored = _default_or_source(session, frame, template)
        else:
            stored = _parse(session, template, raw)
        if stored is None:
            if template.is_required:
                raise ValidationError(_required_message(session, template))
            continue
        row = EventFrameProperty(
            event_frame_id=frame.id,
            event_frame_attribute_template_id=template.id,
            value=stored[0],
            lookup_value_id=stored[1],
            recorded_by_id=recorded_by_id,
        )
        session.add(row)
        written.append(row)
    if written:
        session.flush()
    return written


def update_event_frame_property(
    session: Session,
    event_frame_id: int,
    name: str,
    value: PropertyInput,
    updated_by_id: int | None = None,
) -> EventFrameProperty | None:
    """Set, change or clear one property of a batch.

    A blank value clears an optional property and is refused for a required
    one. Changing a value keeps who first set it and records who changed it;
    giving the value it already holds changes nothing. Returns the row, or
    None once cleared.
    """
    frame = session.get(EventFrame, event_frame_id)
    if frame is None:
        raise NotFoundError(f"no event frame with id {event_frame_id}")
    templates = property_templates(session, frame.event_frame_template_id)
    _check_names(session, frame, templates, {name: value})
    template = next(t for t in templates if t.name == name)
    row = session.scalars(
        select(EventFrameProperty).where(
            EventFrameProperty.event_frame_id == event_frame_id,
            EventFrameProperty.event_frame_attribute_template_id
            == template.id,
        )
    ).first()
    if _is_blank(value):
        if template.is_required:
            raise ValidationError(_required_message(session, template))
        if row is not None:
            session.delete(row)
            session.flush()
        return None
    number, option_id = _parse(session, template, value)
    if row is None:
        row = EventFrameProperty(
            event_frame_id=event_frame_id,
            event_frame_attribute_template_id=template.id,
            value=number,
            lookup_value_id=option_id,
            recorded_by_id=updated_by_id,
        )
        session.add(row)
    elif (row.value, row.lookup_value_id) != (number, option_id):
        row.value = number
        row.lookup_value_id = option_id
        row.updated_by_id = updated_by_id
    session.flush()
    return row


def property_count(session: Session, attribute_template_id: int) -> int:
    """How many batches hold a value for this property."""
    return (
        session.scalar(
            select(func.count())
            .select_from(EventFrameProperty)
            .where(
                EventFrameProperty.event_frame_attribute_template_id
                == attribute_template_id
            )
        )
        or 0
    )


def property_conflicts(session: Session, frame: EventFrame) -> list[str]:
    """Where the batches beer came from into this one disagree on a list.

    Looks one hop upstream: this batch's own source, and each transfer into
    it (the transfer itself if it holds a value on that list, else the
    transfer's source). One hop keeps yeast out: a yeast pitch delivers into
    a wort cooling, not into the fermentation, so its donor is never
    compared. Only lists this batch has a property on are compared.
    """
    templates = property_templates(session, frame.event_frame_template_id)
    lookup_ids = {t.lookup_id for t in templates if t.lookup_id is not None}
    if not lookup_ids:
        return []
    upstream: list[EventFrame] = []
    if frame.source_event_frame_id is not None:
        upstream.append(session.get(EventFrame, frame.source_event_frame_id))
    for transfer in session.scalars(
        select(EventFrame).where(
            EventFrame.destination_event_frame_id == frame.id
        )
    ).all():
        upstream.append(transfer)
    messages = []
    for lookup_id in sorted(lookup_ids):
        seen: dict[str, list[str]] = {}
        for batch in upstream:
            holder = batch
            option = _list_value(session, batch, lookup_id)
            if option is None and batch.source_event_frame_id is not None:
                holder = session.get(EventFrame, batch.source_event_frame_id)
                option = _list_value(session, holder, lookup_id)
            if option is not None:
                seen.setdefault(option, []).append(holder.name)
        if len(seen) > 1:
            parts = []
            for option, names in sorted(seen.items()):
                names = sorted(set(names))
                verb = "is" if len(names) == 1 else "are"
                parts.append(f"{', '.join(names)} {verb} {option}")
            messages.append(
                f"The batches going into {frame.name} don't match: "
                + "; ".join(parts)
                + "."
            )
    return messages


def _list_value(
    session: Session, frame: EventFrame | None, lookup_id: int
) -> str | None:
    """The option this batch holds on a list, if it has a property there."""
    if frame is None:
        return None
    row = session.scalars(
        select(EventFrameProperty)
        .join(
            EventFrameAttributeTemplate,
            EventFrameProperty.event_frame_attribute_template_id
            == EventFrameAttributeTemplate.id,
        )
        .where(
            EventFrameProperty.event_frame_id == frame.id,
            EventFrameAttributeTemplate.lookup_id == lookup_id,
        )
    ).first()
    if row is None:
        return None
    option = session.get(LookupValue, row.lookup_value_id)
    return option.name if option is not None else None


def _default_or_source(
    session: Session, frame: EventFrame, template: EventFrameAttributeTemplate
) -> tuple[float | None, int | None] | None:
    """A blank property's value: the start default, else the source's."""
    if template.default_start_lookup_value_id is not None:
        return None, template.default_start_lookup_value_id
    if template.default_start_value is not None:
        return template.default_start_value, None
    if template.lookup_id is None or frame.source_event_frame_id is None:
        return None
    source = session.get(EventFrame, frame.source_event_frame_id)
    name = _list_value(session, source, template.lookup_id)
    if name is None:
        return None
    option = session.scalars(
        select(LookupValue).where(
            LookupValue.lookup_id == template.lookup_id,
            LookupValue.name == name,
        )
    ).first()
    return None, option.id


def _parse(
    session: Session, template: EventFrameAttributeTemplate, raw
) -> tuple[float | None, int | None]:
    """A given value as stored: (number, option id), exactly one set."""
    if template.lookup_id is not None:
        if not isinstance(raw, str):
            raise ValidationError(
                f"{template.name} is chosen from a list; give an option's "
                "name"
            )
        return None, _resolve_lookup_value(
            session, template.lookup_id, raw.strip()
        )
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{template.name} must be a number") from exc
    if isinstance(raw, bool) or not math.isfinite(number):
        raise ValidationError(f"{template.name} must be a number")
    return number, None


def _is_blank(raw) -> bool:
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _check_names(session, frame, templates, given) -> None:
    known = {t.name for t in templates}
    unknown = sorted(set(given) - known)
    if not unknown:
        return
    batch_type = session.get(EventFrameTemplate, frame.event_frame_template_id)
    has = ", ".join(sorted(known)) if known else "none"
    raise ValidationError(
        f"{batch_type.name} has no property named "
        f"{', '.join(repr(n) for n in unknown)}; its properties: {has}"
    )


def _required_message(
    session: Session, template: EventFrameAttributeTemplate
) -> str:
    batch_type = session.get(
        EventFrameTemplate, template.event_frame_template_id
    )
    return f"A {batch_type.name} needs a {template.name}."
