"""Reading values typed into a form, one measurement or several at once.

A measurement's own page records one reading (``readings``). A batch's page
and a piece of equipment's page take several: one shared time, one box per
measurement, blanks skipped, one press. That is all or nothing, so a bad
value saves none of them, and the page comes back with the message beside
that box and every value kept. Warnings and the one-use pass work as on
every web form (see ``forms``).
"""

from __future__ import annotations

import math
from datetime import datetime

from fastapi import Request
from sqlalchemy.orm import Session

from brewerypi import services
from brewerypi.identity import caller_id
from brewerypi.models import Element, EventFrame, EventFrameTemplate, Tag
from brewerypi.services import (
    NotFoundError,
    NotSelectableError,
    PermissionDeniedError,
    ServiceError,
    ValidationError,
)
from brewerypi.web.forms import (
    accepted_key,
    future_warning,
    input_time,
    mint_token,
    parse_time,
    utc_now,
)

#: A box's form field is this followed by its tag's id.
FIELD_PREFIX = "value_"

_NOT_ON_THE_LIST = "Choose one of the options on the list."


def parse_value(
    session: Session, tag: Tag, raw: str
) -> tuple[float | None, str | None]:
    """Read a form's value for this tag: a number, or a list option."""
    raw = raw.strip()
    if tag.lookup_id is not None:
        if not raw:
            raise ValidationError("Choose a value.")
        return None, raw
    if not raw:
        raise ValidationError("Enter a value.")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError("Enter a number, such as 18.5.") from exc
    if not math.isfinite(value):
        raise ValidationError("Enter a number, such as 18.5.")
    return value, None


def refusal(exc: ServiceError) -> str:
    """A refusal in words for the form it came from."""
    if isinstance(exc, NotSelectableError):
        return _NOT_ON_THE_LIST
    return str(exc)


def window_warnings(
    frame: EventFrame | None, when: datetime, what: str = "this batch"
) -> list[str]:
    """Say when a reading taken for a batch falls outside it."""
    if frame is None:
        return []
    if when < frame.started_at:
        return [f"That time is before {what} started."]
    if frame.ended_at is not None and when > frame.ended_at:
        return [f"That time is after {what} finished."]
    return []


async def form_fields(request: Request) -> dict[str, str]:
    """Every field a form sent, for a form whose boxes aren't known ahead.

    A dependency, so the route itself can stay synchronous.
    """
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


def typed_values(fields: dict[str, str], tags: list[Tag]) -> dict[int, str]:
    """What was typed into each measurement's box, by tag id."""
    return {t.id: fields.get(f"{FIELD_PREFIX}{t.id}", "") for t in tags}


def entry_form(
    request: Request,
    zone: str,
    values: dict[int, str] | None = None,
    observed_at: str | None = None,
    errors: dict[int, str] | None = None,
    error: str | None = None,
    warnings: list[str] = (),
) -> dict:
    """What a page's several-measurements form needs to show itself."""
    warnings = list(warnings)
    return {
        "typed": values or {},
        "errors": errors or {},
        "error": error,
        "warnings": warnings,
        "accepted": accepted_key(warnings),
        "token": mint_token(request),
        "observed_at": (
            input_time(utc_now(), zone) if observed_at is None else observed_at
        ),
        "timezone": zone,
    }


def record_entries(
    session: Session,
    tags: list[Tag],
    fields: dict[str, str],
    zone: str,
    frames: dict[int, EventFrame] | None = None,
    own: EventFrame | None = None,
) -> tuple[dict[int, str], str | None, list[str]]:
    """Record every filled-in box at the shared time.

    ``frames`` names the batch each tag's reading is for, so a time outside
    it warns; ``own`` is the page's batch, called "this batch", and any
    other is one of its steps, called by its type and equipment.

    Returns what stops the save: refusals by tag id, a refusal of the form
    as a whole, and warnings. The caller commits only when all three are
    empty, or the warnings were accepted, and rolls back otherwise.
    """
    try:
        when = parse_time(fields.get("observed_at", ""), zone, "reading's")
    except ValidationError as exc:
        return {}, str(exc), []
    filled = [
        (tag_id, raw.strip())
        for tag_id, raw in typed_values(fields, tags).items()
        if raw.strip()
    ]
    if not filled:
        return {}, "Enter at least one value.", []
    by_id = {t.id: t for t in tags}
    errors: dict[int, str] = {}
    for tag_id, raw in filled:
        tag = by_id[tag_id]
        try:
            number, option = parse_value(session, tag, raw)
            services.record_tag_value(
                session,
                tag.id,
                when,
                number,
                option,
                recorded_by_id=caller_id(),
            )
        except (NotFoundError, PermissionDeniedError):
            raise
        except ServiceError as exc:
            errors[tag.id] = refusal(exc)
    if errors:
        return errors, None, []
    warnings = future_warning(when, "That time")
    for tag_id, _ in filled:
        frame = (frames or {}).get(tag_id)
        called = _called(session, frame, own)
        for warning in window_warnings(frame, when, called):
            if warning not in warnings:
                warnings.append(warning)
    return {}, None, warnings


def _called(
    session: Session, frame: EventFrame | None, own: EventFrame | None
) -> str:
    """How a window warning names the batch a reading is for."""
    if frame is None or own is None or frame.id == own.id:
        return "this batch"
    template = session.get(EventFrameTemplate, frame.event_frame_template_id)
    element = session.get(Element, frame.element_id)
    return f"{template.name} on {element.name}"
