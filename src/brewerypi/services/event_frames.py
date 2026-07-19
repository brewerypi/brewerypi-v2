"""Service-layer lifecycle for event frames (batch windows).

A frame is a half-open interval ``[started_at, ended_at)`` on one element; a
NULL ``ended_at`` means it is still running. Times are naive UTC; the tool
boundary converts.

Structural rules:

* The frame's element must instance the event frame template's element
  template.
* Nesting mirrors the templates (rule "A1"): a child template's frame needs a
  parent frame instancing the template's parent template, and that parent
  frame's element must be the child element's parent element.
* A child frame's window must sit within its parent's window.
* Overlap: if the element's template is ``exclusive`` (single-occupancy), no
  two frames on that element may overlap in time, across any template. A
  non-exclusive element (an umbrella like a brewhouse) allows unlimited
  concurrency.

Opening a frame writes each attribute's default *start* value as a reading on
the wired tag at ``started_at``; closing writes the default *end* value at
``ended_at`` (and closes any still-open descendants at the same instant,
matching upstream BreweryPi). Moving a boundary later leaves already-written
readings where they are -- they are independent facts, and the frame is only a
window over them.

Each function takes an open Session and raises the service exceptions on rule
violations. Callers own the transaction; these functions ``flush`` but never
commit.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from brewerypi.models import (
    Element,
    ElementTemplate,
    EventFrame,
    EventFrameAttribute,
    EventFrameAttributeTemplate,
    EventFrameTemplate,
    TagValue,
)
from brewerypi.services._validation import clean_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

# Sentinel: distinguishes "leave unchanged" from "set to None" on update.
_UNSET = object()


def list_event_frames(
    session: Session,
    element_id: int | None = None,
    event_frame_template_id: int | None = None,
    parent_id: int | None = None,
    open_only: bool = False,
) -> list[EventFrame]:
    """Return event frames, newest first, optionally filtered."""
    stmt = select(EventFrame).order_by(EventFrame.started_at.desc())
    if element_id is not None:
        stmt = stmt.where(EventFrame.element_id == element_id)
    if event_frame_template_id is not None:
        stmt = stmt.where(
            EventFrame.event_frame_template_id == event_frame_template_id
        )
    if parent_id is not None:
        stmt = stmt.where(EventFrame.parent_id == parent_id)
    if open_only:
        stmt = stmt.where(EventFrame.ended_at.is_(None))
    return list(session.scalars(stmt).all())


def get_event_frame(session: Session, event_frame_id: int) -> EventFrame:
    """Return one event frame, or raise NotFoundError."""
    frame = session.get(EventFrame, event_frame_id)
    if frame is None:
        raise NotFoundError(f"no event frame with id {event_frame_id}")
    return frame


def create_event_frame(
    session: Session,
    element_id: int,
    event_frame_template_id: int,
    name: str,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    parent_id: int | None = None,
) -> EventFrame:
    """Start an event frame on an element.

    ``started_at`` defaults to now (UTC). Leave ``ended_at`` unset for a
    running frame. Writes each attribute's default start value at
    ``started_at``.
    """
    name = clean_str(name, "name", 45)
    if started_at is None:
        started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    _check_window(started_at, ended_at)
    element = session.get(Element, element_id)
    if element is None:
        raise NotFoundError(f"no element with id {element_id}")
    template = session.get(EventFrameTemplate, event_frame_template_id)
    if template is None:
        raise NotFoundError(
            f"no event frame template with id {event_frame_template_id}"
        )
    if element.element_template_id != template.element_template_id:
        raise ValidationError(
            f"element {element_id} does not instance the event frame "
            f"template's element template ({template.element_template_id})"
        )
    _check_parent(session, element, template, parent_id, started_at, ended_at)
    _check_overlap(session, element, started_at, ended_at)
    frame = EventFrame(
        element_id=element_id,
        event_frame_template_id=event_frame_template_id,
        parent_id=parent_id,
        name=name,
        started_at=started_at,
        ended_at=ended_at,
    )
    session.add(frame)
    session.flush()
    _write_boundary_values(session, frame, "start", started_at)
    if ended_at is not None:
        _write_boundary_values(session, frame, "end", ended_at)
    return frame


def close_event_frame(
    session: Session, event_frame_id: int, ended_at: datetime | None = None
) -> EventFrame:
    """Close a running frame, writing its default end values.

    ``ended_at`` defaults to now (UTC). Any still-open descendant frames are
    closed at the same instant (matching upstream), each writing its own end
    values, which also keeps every child within its parent's window.
    """
    frame = get_event_frame(session, event_frame_id)
    if frame.ended_at is not None:
        raise ValidationError(
            f"event frame {event_frame_id} is already closed "
            f"(ended {frame.ended_at.isoformat()})"
        )
    if ended_at is None:
        ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for open_frame in _open_self_and_descendants(session, frame):
        _check_window(open_frame.started_at, ended_at)
        _check_overlap(
            session,
            session.get(Element, open_frame.element_id),
            open_frame.started_at,
            ended_at,
            exclude_id=open_frame.id,
        )
        open_frame.ended_at = ended_at
        session.flush()
        _write_boundary_values(session, open_frame, "end", ended_at)
    return frame


def reopen_event_frame(
    session: Session, event_frame_id: int
) -> EventFrame:
    """Reopen a closed frame (clear ``ended_at``), leaving readings alone.

    Re-runs the overlap and containment guards: reopening extends the window
    to +infinity, which can now collide with a frame started afterwards, or
    push a child past a closed parent's end. Values written at the old end
    stay put -- correcting them is the operator's call. Often the better fix
    is ``update_event_frame`` with a corrected ``ended_at``.
    """
    frame = get_event_frame(session, event_frame_id)
    if frame.ended_at is None:
        raise ValidationError(
            f"event frame {event_frame_id} is already open"
        )
    _check_overlap(
        session,
        session.get(Element, frame.element_id),
        frame.started_at,
        None,
        exclude_id=frame.id,
    )
    _check_within_parent(session, frame, frame.started_at, None)
    frame.ended_at = None
    session.flush()
    return frame


def update_event_frame(
    session: Session,
    event_frame_id: int,
    name: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = _UNSET,  # type: ignore[assignment]
) -> EventFrame:
    """Update a frame's name and/or window.

    Moving a boundary re-runs the overlap and containment guards (including
    that children still fit). Readings already written at the old boundaries
    stay where they are.
    """
    frame = get_event_frame(session, event_frame_id)
    if name is not None:
        frame.name = clean_str(name, "name", 45)
    new_start = frame.started_at if started_at is None else started_at
    new_end = frame.ended_at if ended_at is _UNSET else ended_at
    if started_at is not None or ended_at is not _UNSET:
        _check_window(new_start, new_end)
        _check_overlap(
            session,
            session.get(Element, frame.element_id),
            new_start,
            new_end,
            exclude_id=frame.id,
        )
        _check_within_parent(session, frame, new_start, new_end)
        _check_children_fit(session, frame, new_start, new_end)
        frame.started_at = new_start
        frame.ended_at = new_end
    session.flush()
    return frame


def delete_event_frame(session: Session, event_frame_id: int) -> None:
    """Delete a frame and its child frames.

    Tags, their readings (including boundary values written by this frame),
    and the element's attribute wiring are all left untouched -- a frame is a
    window over data it does not own.
    """
    frame = get_event_frame(session, event_frame_id)
    session.delete(frame)
    session.flush()


def _check_window(
    started_at: datetime, ended_at: datetime | None
) -> None:
    if ended_at is not None and ended_at <= started_at:
        raise ValidationError(
            "ended_at must be after started_at"
        )


def _check_parent(
    session: Session,
    element: Element,
    template: EventFrameTemplate,
    parent_id: int | None,
    started_at: datetime,
    ended_at: datetime | None,
) -> None:
    """Enforce the A1 instance mirror and containment for a new frame."""
    if template.parent_id is None:
        if parent_id is not None:
            raise ValidationError(
                "this frame's template is top-level, so the frame must be "
                "top-level (no parent)"
            )
        return
    if parent_id is None:
        raise ValidationError(
            "this frame's template has a parent template, so the frame "
            f"needs a parent frame instancing template {template.parent_id}"
        )
    parent = session.get(EventFrame, parent_id)
    if parent is None:
        raise NotFoundError(f"no event frame with id {parent_id}")
    if parent.event_frame_template_id != template.parent_id:
        raise ValidationError(
            f"parent frame {parent_id} must instance event frame template "
            f"{template.parent_id} (this template's parent template)"
        )
    if element.parent_id != parent.element_id:
        raise ValidationError(
            f"element {element.id} must be a child of the parent frame's "
            f"element ({parent.element_id})"
        )
    _check_contains(parent, started_at, ended_at)


def _check_within_parent(
    session: Session,
    frame: EventFrame,
    started_at: datetime,
    ended_at: datetime | None,
) -> None:
    if frame.parent_id is None:
        return
    parent = session.get(EventFrame, frame.parent_id)
    if parent is not None:
        _check_contains(parent, started_at, ended_at)


def _check_contains(
    parent: EventFrame,
    started_at: datetime,
    ended_at: datetime | None,
) -> None:
    """A child's window must sit within its parent's."""
    if started_at < parent.started_at:
        raise ValidationError(
            f"frame starts before its parent frame {parent.id} "
            f"({parent.started_at.isoformat()})"
        )
    if parent.ended_at is not None:
        if ended_at is None:
            raise ValidationError(
                f"parent frame {parent.id} is closed, so this frame cannot "
                "be left open"
            )
        if ended_at > parent.ended_at:
            raise ValidationError(
                f"frame ends after its parent frame {parent.id} "
                f"({parent.ended_at.isoformat()})"
            )


def _check_children_fit(
    session: Session,
    frame: EventFrame,
    started_at: datetime,
    ended_at: datetime | None,
) -> None:
    """Moving a parent's window must not orphan its children."""
    children = session.scalars(
        select(EventFrame).where(EventFrame.parent_id == frame.id)
    ).all()
    for child in children:
        if child.started_at < started_at:
            raise ValidationError(
                f"child frame {child.id} starts before the new window"
            )
        if ended_at is not None and (
            child.ended_at is None or child.ended_at > ended_at
        ):
            raise ValidationError(
                f"child frame {child.id} would extend past the new end"
            )


def _check_overlap(
    session: Session,
    element: Element | None,
    started_at: datetime,
    ended_at: datetime | None,
    exclude_id: int | None = None,
) -> None:
    """Single-occupancy elements refuse overlapping frames (any template)."""
    if element is None:
        return
    element_template = session.get(
        ElementTemplate, element.element_template_id
    )
    if element_template is None or not element_template.exclusive:
        return
    stmt = select(EventFrame).where(
        EventFrame.element_id == element.id,
        # existing ends after we start (NULL end = still running)
        or_(
            EventFrame.ended_at.is_(None),
            EventFrame.ended_at > started_at,
        ),
    )
    if ended_at is not None:
        # ...and existing starts before we end (half-open: touching is fine)
        stmt = stmt.where(EventFrame.started_at < ended_at)
    if exclude_id is not None:
        stmt = stmt.where(EventFrame.id != exclude_id)
    clash = session.scalars(stmt).first()
    if clash is not None:
        end_text = (
            clash.ended_at.isoformat()
            if clash.ended_at is not None
            else "open"
        )
        raise ConflictError(
            f"element {element.name!r} is single-occupancy and already has "
            f"event frame {clash.id} ({clash.name!r}, "
            f"{clash.started_at.isoformat()} to {end_text}) overlapping "
            "this window"
        )


def _open_self_and_descendants(
    session: Session, frame: EventFrame
) -> list[EventFrame]:
    """The frame plus every still-open descendant, parents first."""
    found = [frame]
    frontier = [frame.id]
    while frontier:
        children = session.scalars(
            select(EventFrame).where(
                EventFrame.parent_id.in_(frontier),
                EventFrame.ended_at.is_(None),
            )
        ).all()
        if not children:
            break
        found.extend(children)
        frontier = [child.id for child in children]
    return found


def _write_boundary_values(
    session: Session,
    frame: EventFrame,
    which: str,
    at: datetime,
) -> list[TagValue]:
    """Write the template's default start/end values as readings.

    Resolves frame -> element -> wiring -> tag. Attributes with no default
    configured, or with no wiring on the element (no tag area), are skipped.
    """
    templates = session.scalars(
        select(EventFrameAttributeTemplate).where(
            EventFrameAttributeTemplate.event_frame_template_id
            == frame.event_frame_template_id
        )
    ).all()
    written: list[TagValue] = []
    for template in templates:
        if which == "start":
            value = template.default_start_value
            lookup_value_id = template.default_start_lookup_value_id
        else:
            value = template.default_end_value
            lookup_value_id = template.default_end_lookup_value_id
        if value is None and lookup_value_id is None:
            continue
        wiring = session.scalars(
            select(EventFrameAttribute).where(
                EventFrameAttribute.element_id == frame.element_id,
                EventFrameAttribute.event_frame_attribute_template_id
                == template.id,
            )
        ).first()
        if wiring is None:
            continue
        reading = TagValue(
            tag_id=wiring.tag_id,
            observed_at=at,
            value=value,
            lookup_value_id=lookup_value_id,
        )
        session.add(reading)
        written.append(reading)
    if written:
        session.flush()
    return written


def event_frame_reading_count(
    session: Session, event_frame_id: int, tag_id: int
) -> int:
    """Count readings on a tag inside a frame's window (a frame IS a lens)."""
    frame = get_event_frame(session, event_frame_id)
    stmt = (
        select(func.count())
        .select_from(TagValue)
        .where(
            TagValue.tag_id == tag_id,
            TagValue.observed_at >= frame.started_at,
        )
    )
    if frame.ended_at is not None:
        stmt = stmt.where(TagValue.observed_at <= frame.ended_at)
    return session.scalar(stmt) or 0
