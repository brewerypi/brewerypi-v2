"""Batch lineage: walking the flow graph built by transfer links.

Every ``EventFrame`` may carry a ``source_event_frame_id`` and a
``destination_event_frame_id``, so the frame holding a link IS the transfer.
That makes the graph uniform whether or not dedicated transfer equipment is
involved: a centrifugation frame with both links reads as
``fermentation -> centrifugation -> conditioning``, while a direct rack that
just sets ``source`` on the conditioning frame reads as
``fermentation -> conditioning``. Both are walked by the same neighbour rule.

The walk MUST carry a visited set. The flow graph genuinely cycles -- a brite
tank can send beer back to the fermentation it came from, and because that is
the same batch it points at the *original* frame -- so an unguarded walk loops
forever. This is load-bearing, not defensive.

Results are flat: every frame reached, each with the depth at which it was
first seen, plus the edges between them. A cyclic or re-blending graph has no
faithful tree form, so none is offered. ``truncated`` says whether
``max_depth`` cut the walk short, so a caller never mistakes a clipped graph
for a complete one.

Times are naive UTC; the tool boundary converts.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi.models import Element, EventFrame, EventFrameTemplate
from brewerypi.services.event_frames import get_event_frame
from brewerypi.services.exceptions import ValidationError

DIRECTIONS = ("upstream", "downstream", "both")
MAX_DEPTH_LIMIT = 50


def trace_lineage(
    session: Session,
    event_frame_id: int,
    direction: str = "both",
    max_depth: int = 10,
) -> dict:
    """Walk the transfer graph from one batch.

    ``upstream`` follows beer back towards where it came from (the question
    "which brews are in this canning run?"), ``downstream`` follows it forward
    to where it ended up, ``both`` does each and merges. Returns the frames
    reached (with ``depth``, 0 being the starting batch) and the transfer
    edges between them.
    """
    if direction not in DIRECTIONS:
        raise ValidationError(
            f"direction must be one of {', '.join(DIRECTIONS)}"
        )
    if max_depth < 1 or max_depth > MAX_DEPTH_LIMIT:
        raise ValidationError(
            f"max_depth must be between 1 and {MAX_DEPTH_LIMIT}"
        )
    start = get_event_frame(session, event_frame_id)

    visited = {start.id: 0}
    frames = [_frame_dict(session, start, 0)]
    edges: dict[tuple[int, int, int], dict] = {}
    frontier = [start]
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        next_frontier = []
        for frame in frontier:
            for neighbour, edge in _neighbours(session, frame, direction):
                edges.setdefault(_edge_key(edge), edge)
                if neighbour.id in visited:
                    continue
                visited[neighbour.id] = depth
                frames.append(_frame_dict(session, neighbour, depth))
                next_frontier.append(neighbour)
        frontier = next_frontier

    truncated = any(
        neighbour.id not in visited
        for frame in frontier
        for neighbour, _ in _neighbours(session, frame, direction)
    )
    return {
        "event_frame_id": start.id,
        "direction": direction,
        "max_depth": max_depth,
        "frames": frames,
        "edges": list(edges.values()),
        "truncated": truncated,
    }


def _neighbours(
    session: Session, frame: EventFrame, direction: str
) -> list[tuple[EventFrame, dict]]:
    """Frames one flow hop away, with the edge that got us there.

    Two shapes per direction: the link this frame carries itself, and the
    links other frames carry pointing at it.
    """
    found: list[tuple[EventFrame, dict]] = []
    if direction in ("upstream", "both"):
        if frame.source_event_frame_id is not None:
            source = session.get(EventFrame, frame.source_event_frame_id)
            if source is not None:
                found.append((source, _edge(frame, source, frame)))
        for other in _linking_to(session, frame.id, "destination"):
            found.append((other, _edge(other, other, frame)))
    if direction in ("downstream", "both"):
        if frame.destination_event_frame_id is not None:
            destination = session.get(
                EventFrame, frame.destination_event_frame_id
            )
            if destination is not None:
                found.append(
                    (destination, _edge(frame, frame, destination))
                )
        for other in _linking_to(session, frame.id, "source"):
            found.append((other, _edge(other, frame, other)))
    return found


def _linking_to(
    session: Session, frame_id: int, role: str
) -> list[EventFrame]:
    column = (
        EventFrame.source_event_frame_id
        if role == "source"
        else EventFrame.destination_event_frame_id
    )
    return list(session.scalars(select(EventFrame).where(column == frame_id)))


def _edge(
    transfer: EventFrame, from_frame: EventFrame, to_frame: EventFrame
) -> dict:
    """One flow hop. ``transfer`` is the frame that carries the link."""
    return {
        "transfer_event_frame_id": transfer.id,
        "transfer_name": transfer.name,
        "from_event_frame_id": from_frame.id,
        "to_event_frame_id": to_frame.id,
    }


def _edge_key(edge: dict) -> tuple[int, int, int]:
    return (
        edge["transfer_event_frame_id"],
        edge["from_event_frame_id"],
        edge["to_event_frame_id"],
    )


def _frame_dict(session: Session, frame: EventFrame, depth: int) -> dict:
    element = session.get(Element, frame.element_id)
    template = session.get(EventFrameTemplate, frame.event_frame_template_id)
    return {
        "id": frame.id,
        "name": frame.name,
        "depth": depth,
        "element_id": frame.element_id,
        "element_name": element.name if element is not None else None,
        "event_frame_template_id": frame.event_frame_template_id,
        "event_frame_template_name": (
            template.name if template is not None else None
        ),
        "started_at": frame.started_at,
        "ended_at": frame.ended_at,
        "open": frame.ended_at is None,
        "source_event_frame_id": frame.source_event_frame_id,
        "destination_event_frame_id": frame.destination_event_frame_id,
    }
