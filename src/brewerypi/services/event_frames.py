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

Flow rules (a frame with a source and/or destination is a *transfer*: beer
moving out of one batch and into another):

* Both endpoints are optional -- a packaging run has a source and no
  destination, and a destination is often set only once the receiving batch
  exists (``link_transfer``).
* An endpoint must be a different frame, in the same site, and source and
  destination must differ from each other.
* Containment, both ends: the transfer's window must sit *within* each
  endpoint's window (an open endpoint extends to +infinity). Beer cannot leave
  a batch that had not started or had already finished. This makes frame
  boundaries a configuration convention -- a brew must stay open until its
  knockout finishes, and the receiving fermentation must already be open.
* Containment is re-checked whenever an endpoint's window moves or closes, so
  the database never holds a link the create path would have rejected. An open
  transfer runs to +infinity, so closing a batch it still flows through is
  refused until the transfer itself is closed.
* Flow is NOT containment: ``parent_id`` nests (a Mashing inside a Brew) under
  the A1 mirror rule, while flow runs between siblings and may legitimately
  cycle -- a brite tank can send beer back to the fermentation it came from,
  which is the same batch and therefore the same frame. Lineage walks
  (``services.lineage``) must carry a visited set.

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

from collections.abc import Collection
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
    element_template_id: int | None = None,
    source_event_frame_id: int | None = None,
    destination_event_frame_id: int | None = None,
) -> list[EventFrame]:
    """Return event frames, newest first, optionally filtered.

    ``element_template_id`` narrows to frames on elements instancing that
    template ("every fermentation-vessel batch"), which is how a caller
    populates a source/destination picker. The two flow filters answer the
    other half: what moved out of, or into, a given batch.
    """
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
    if element_template_id is not None:
        stmt = stmt.where(
            EventFrame.element_id.in_(
                select(Element.id).where(
                    Element.element_template_id == element_template_id
                )
            )
        )
    if source_event_frame_id is not None:
        stmt = stmt.where(
            EventFrame.source_event_frame_id == source_event_frame_id
        )
    if destination_event_frame_id is not None:
        stmt = stmt.where(
            EventFrame.destination_event_frame_id
            == destination_event_frame_id
        )
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
    source_event_frame_id: int | None = None,
    destination_event_frame_id: int | None = None,
) -> EventFrame:
    """Start an event frame on an element.

    ``started_at`` defaults to now (UTC). Leave ``ended_at`` unset for a
    running frame. Writes each attribute's default start value at
    ``started_at``.

    Passing a source and/or destination makes this frame a transfer: beer
    moving out of one batch and into another. Either may be filled in later
    with ``link_transfer``.
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
    _check_transfer_links(
        session,
        None,
        element,
        source_event_frame_id,
        destination_event_frame_id,
        started_at,
        ended_at,
    )
    frame = EventFrame(
        element_id=element_id,
        event_frame_template_id=event_frame_template_id,
        parent_id=parent_id,
        name=name,
        started_at=started_at,
        ended_at=ended_at,
        source_event_frame_id=source_event_frame_id,
        destination_event_frame_id=destination_event_frame_id,
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

    Refused while beer is still moving: a batch cannot close while a transfer
    out of (or into) it is still running, and a transfer cannot close after
    the batch it flows through has ended.
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
        _check_transfer_links(
            session,
            open_frame.id,
            session.get(Element, open_frame.element_id),
            open_frame.source_event_frame_id,
            open_frame.destination_event_frame_id,
            open_frame.started_at,
            ended_at,
        )
        _check_linked_transfers_fit(
            session, open_frame, open_frame.started_at, ended_at
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
    to +infinity, which can now collide with a frame started afterwards, push
    a child past a closed parent's end, or -- for a transfer -- run past the
    end of a batch it flows through. Reopening only ever widens a window, so
    transfers pointing *at* this frame always still fit. Values written at the
    old end stay put -- correcting them is the operator's call. Often the
    better fix is ``update_event_frame`` with a corrected ``ended_at``.
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
    _check_transfer_links(
        session,
        frame.id,
        session.get(Element, frame.element_id),
        frame.source_event_frame_id,
        frame.destination_event_frame_id,
        frame.started_at,
        None,
    )
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
    that children still fit, that this frame still sits inside any batch it
    transfers between, and that every transfer linked to this frame still sits
    inside the new window). Readings already written at the old boundaries
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
        _check_transfer_links(
            session,
            frame.id,
            session.get(Element, frame.element_id),
            frame.source_event_frame_id,
            frame.destination_event_frame_id,
            new_start,
            new_end,
        )
        _check_linked_transfers_fit(session, frame, new_start, new_end)
        frame.started_at = new_start
        frame.ended_at = new_end
    session.flush()
    return frame


def link_transfer(
    session: Session,
    event_frame_id: int,
    source_event_frame_id: int | None = _UNSET,  # type: ignore[assignment]
    destination_event_frame_id: (
        int | None
    ) = _UNSET,  # type: ignore[assignment]
) -> EventFrame:
    """Set or clear this frame's transfer source and/or destination.

    The destination is routinely set here rather than at creation: a transfer
    often starts before anyone has opened the receiving batch. Pass ``None``
    for either endpoint to clear it; omit it to leave it alone. The full set
    of flow rules is re-checked against whatever the frame ends up with.
    """
    frame = get_event_frame(session, event_frame_id)
    source = (
        frame.source_event_frame_id
        if source_event_frame_id is _UNSET
        else source_event_frame_id
    )
    destination = (
        frame.destination_event_frame_id
        if destination_event_frame_id is _UNSET
        else destination_event_frame_id
    )
    _check_transfer_links(
        session,
        frame.id,
        session.get(Element, frame.element_id),
        source,
        destination,
        frame.started_at,
        frame.ended_at,
    )
    frame.source_event_frame_id = source
    frame.destination_event_frame_id = destination
    session.flush()
    return frame


def delete_event_frame(session: Session, event_frame_id: int) -> None:
    """Delete a frame and its child frames.

    Refused while a transfer outside the deleted subtree still points at any
    frame inside it: lineage is the one thing a delete must not quietly
    destroy. Unlink the transfer (or delete it) first. A transfer that lives
    inside the subtree goes with it.

    Tags, their readings (including boundary values written by this frame),
    and the element's attribute wiring are all left untouched -- a frame is a
    window over data it does not own.
    """
    frame = get_event_frame(session, event_frame_id)
    doomed = _subtree_ids(session, frame.id)
    blockers = transfers_blocking_delete(session, event_frame_id)
    if blockers:
        first = blockers[0]
        role = (
            "source"
            if first.source_event_frame_id in doomed
            else "destination"
        )
        extra = (
            f" (and {len(blockers) - 1} other transfer(s))"
            if len(blockers) > 1
            else ""
        )
        raise ConflictError(
            f"cannot delete event frame {event_frame_id}: it is the {role} "
            f"of transfer frame {first.id} ({first.name!r}){extra}; unlink "
            "the transfer first"
        )
    session.delete(frame)
    session.flush()


def transfers_blocking_delete(
    session: Session, event_frame_id: int
) -> list[EventFrame]:
    """Transfers that would be orphaned by deleting this frame's subtree.

    Callers preview with this before asking for confirmation, so the refusal
    is visible before the operator commits to the delete rather than after.
    """
    doomed = _subtree_ids(session, event_frame_id)
    return transfers_referencing(session, doomed, exclude_ids=doomed)


def transfers_referencing(
    session: Session,
    frame_ids: Collection[int],
    exclude_ids: Collection[int] = (),
) -> list[EventFrame]:
    """Return transfer frames pointing at any of ``frame_ids``.

    ``exclude_ids`` drops transfers that are themselves being deleted. Public
    because the same question guards deleting an element (which cascades its
    frames).
    """
    ids = list(frame_ids)
    if not ids:
        return []
    stmt = select(EventFrame).where(
        or_(
            EventFrame.source_event_frame_id.in_(ids),
            EventFrame.destination_event_frame_id.in_(ids),
        )
    )
    excluded = list(exclude_ids)
    if excluded:
        stmt = stmt.where(EventFrame.id.not_in(excluded))
    return list(session.scalars(stmt).all())


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


def _site_id_for_element(
    session: Session, element: Element | None
) -> int | None:
    if element is None:
        return None
    template = session.get(ElementTemplate, element.element_template_id)
    return template.site_id if template is not None else None


def _check_transfer_links(
    session: Session,
    frame_id: int | None,
    element: Element | None,
    source_id: int | None,
    destination_id: int | None,
    started_at: datetime,
    ended_at: datetime | None,
) -> None:
    """Enforce the flow rules for a frame's source/destination links."""
    if source_id is not None and source_id == destination_id:
        raise ValidationError(
            "a transfer's source and destination must be different batches"
        )
    site_id = _site_id_for_element(session, element)
    for role, endpoint_id in (
        ("source", source_id),
        ("destination", destination_id),
    ):
        if endpoint_id is None:
            continue
        if frame_id is not None and endpoint_id == frame_id:
            raise ValidationError(
                f"a batch cannot be its own transfer {role}"
            )
        endpoint = session.get(EventFrame, endpoint_id)
        if endpoint is None:
            raise NotFoundError(f"no event frame with id {endpoint_id}")
        endpoint_site = _site_id_for_element(
            session, session.get(Element, endpoint.element_id)
        )
        if site_id is not None and endpoint_site != site_id:
            raise ValidationError(
                f"the {role} batch {endpoint.id} ({endpoint.name!r}) is at "
                "another site; beer does not transfer between sites"
            )
        _check_transfer_fits(endpoint, role, started_at, ended_at)


def _check_transfer_fits(
    endpoint: EventFrame,
    role: str,
    started_at: datetime,
    ended_at: datetime | None,
) -> None:
    """A transfer's window must sit inside the batch it flows through.

    An open endpoint runs to +infinity, so it constrains only the start; an
    open transfer runs to +infinity too, so it needs an open endpoint.
    """
    if started_at < endpoint.started_at:
        raise ValidationError(
            f"the transfer starts before its {role} batch {endpoint.id} "
            f"({endpoint.name!r}, {endpoint.started_at.isoformat()}); the "
            f"{role} batch must already be running when beer moves"
        )
    if endpoint.ended_at is None:
        return
    if ended_at is None:
        raise ValidationError(
            f"the {role} batch {endpoint.id} ({endpoint.name!r}) is already "
            "closed, so this transfer cannot be left open"
        )
    if ended_at > endpoint.ended_at:
        raise ValidationError(
            f"the transfer ends after its {role} batch {endpoint.id} "
            f"({endpoint.name!r}, {endpoint.ended_at.isoformat()}); the "
            f"{role} batch must stay open until the transfer finishes"
        )


def _check_linked_transfers_fit(
    session: Session,
    frame: EventFrame,
    started_at: datetime,
    ended_at: datetime | None,
) -> None:
    """Moving or closing a batch must not strand a transfer through it."""
    for transfer in transfers_referencing(session, [frame.id], [frame.id]):
        role = (
            "out of"
            if transfer.source_event_frame_id == frame.id
            else "into"
        )
        if ended_at is not None and transfer.ended_at is None:
            raise ConflictError(
                f"transfer frame {transfer.id} ({transfer.name!r}) is still "
                f"running {role} this batch; close the transfer first"
            )
        if transfer.started_at < started_at:
            raise ValidationError(
                f"transfer frame {transfer.id} ({transfer.name!r}) starts "
                f"{transfer.started_at.isoformat()}, before the new start"
            )
        if (
            ended_at is not None
            and transfer.ended_at is not None
            and transfer.ended_at > ended_at
        ):
            raise ValidationError(
                f"transfer frame {transfer.id} ({transfer.name!r}) runs "
                f"{role} this batch until "
                f"{transfer.ended_at.isoformat()}, past the new end"
            )


def _subtree_ids(session: Session, frame_id: int) -> set[int]:
    """The frame's id plus every descendant's (containment, not flow)."""
    ids = {frame_id}
    frontier = [frame_id]
    while frontier:
        children = [
            child_id
            for child_id in session.scalars(
                select(EventFrame.id).where(
                    EventFrame.parent_id.in_(frontier)
                )
            ).all()
            if child_id not in ids
        ]
        if not children:
            break
        ids.update(children)
        frontier = children
    return ids


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
