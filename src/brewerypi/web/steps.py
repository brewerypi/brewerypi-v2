"""A batch's steps, run from the batch's own page.

A brew's steps (Mashing, Lautering, Boiling) are batch types nested under
the brew's, each run on equipment of its kind inside the brewhouse. Knowing
the brew, the page knows the rest: the step's type, its equipment (the one
piece of that kind inside, or a short choice when there are several), its
parent, and its name, which is the brew's. So starting or finishing a step
is one press with the section's shared time, and nothing else finishes
along with it: steps often overlap.

A step whose type has a required property with no default can't be started
without a choice, so its button opens the start form with the rest filled
in. Finishing a step that has running steps of its own opens the close
page, which lists what closes with it.

The page is one form with one time. Each step run's measurements sit
under it: boxes while it runs, a summary once it has finished. Start and
Finish save whatever was typed in the boxes too, at the same time and all
or nothing, so a mash-out temperature and Finish Mashing are one press;
Record readings saves the boxes alone (see ``entry``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from brewerypi import access, services
from brewerypi.access import OPERATE
from brewerypi.identity import CurrentUser, caller_id
from brewerypi.models import (
    Element,
    ElementTemplate,
    EventFrame,
    EventFrameTemplate,
    Tag,
)
from brewerypi.serializers import event_frame_dict
from brewerypi.services import NotFoundError, ValidationError
from brewerypi.web.measurements import batch_measurements


def _needs_form(session: Session, template: EventFrameTemplate) -> bool:
    """Whether starting this step needs a choice the page can't make.

    A required property with no start default needs one, unless the step
    draws from its parent, which then supplies it.
    """
    if template.parent_flow == "source":
        return False
    return any(
        t.is_required
        and t.default_start_value is None
        and t.default_start_lookup_value_id is None
        for t in services.property_templates(session, template.id)
    )


def _busy_with(session: Session, element: Element) -> EventFrame | None:
    """The batch keeping single-batch equipment busy, if any.

    Counted whoever is looking, as on the site page: whether a vessel is
    free doesn't depend on who asks.
    """
    kind = session.get(ElementTemplate, element.element_template_id)
    if kind.exclusive is False:
        return None
    running = services.list_event_frames(
        session, element_id=element.id, open_only=True
    )
    return running[0] if running else None


def _run(session: Session, frame: EventFrame) -> dict:
    """One run of a step, its times local as the serializer gives them."""
    row = event_frame_dict(session, frame)
    return {
        "id": frame.id,
        "name": frame.name,
        "element_name": row["element_name"],
        "open": frame.ended_at is None,
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "has_open_steps": bool(
            services.list_event_frames(
                session, parent_id=frame.id, open_only=True
            )
        ),
    }


def _place(session: Session, frame: EventFrame, element: Element) -> dict:
    """Equipment a step could start on, and what keeps it busy.

    Busy with one of this batch's own steps, it has a Finish button on the
    same row, so it says nothing more.
    """
    busy = _busy_with(session, element)
    return {
        "id": element.id,
        "name": element.name,
        "busy": busy.name if busy is not None else None,
        "busy_here": busy is not None and busy.parent_id == frame.id,
    }


def step_rows(
    session: Session, user: CurrentUser, frame: EventFrame
) -> list[dict]:
    """One row per step the batch's type defines, in step order.

    Each row lists the step's runs in this batch and the equipment it could
    start on. ``next`` marks the first step that hasn't run and has
    somewhere free to run: the button most likely pressed next.
    """
    inside = access.visible(
        session,
        user,
        services.list_elements(session, parent_id=frame.element_id),
    )
    runs = access.visible(
        session, user, services.list_event_frames(session, parent_id=frame.id)
    )
    rows = []
    for template in services.list_event_frame_templates(
        session, parent_id=frame.event_frame_template_id
    ):
        kind = session.get(ElementTemplate, template.element_template_id)
        rows.append(
            {
                "id": template.id,
                "order": template.step_order,
                "name": template.name,
                "kind": kind.name,
                "needs_form": _needs_form(session, template),
                "runs": [
                    _run(session, r)
                    for r in sorted(runs, key=lambda r: r.started_at)
                    if r.event_frame_template_id == template.id
                ],
                "places": [
                    _place(session, frame, e)
                    for e in inside
                    if e.element_template_id == template.element_template_id
                ],
                "next": False,
            }
        )
    for row in rows:
        if not row["runs"] and any(not p["busy"] for p in row["places"]):
            row["next"] = True
            break
    return rows


def _parse_action(action: str) -> tuple[str, list[int]]:
    verb, _, rest = action.partition(":")
    try:
        numbers = [int(part) for part in rest.split(":")] if rest else []
    except ValueError:
        numbers = []
    if (verb, len(numbers)) not in (("start", 2), ("finish", 1)):
        raise NotFoundError(f"no step action {action!r}")
    return verb, numbers


def _own_step_type(
    session: Session, frame: EventFrame, template_id: int
) -> EventFrameTemplate:
    template = session.get(EventFrameTemplate, template_id)
    if template is None or template.parent_id != frame.event_frame_template_id:
        raise NotFoundError(f"no step {template_id} in this batch")
    return template


def _own_step(session: Session, frame: EventFrame, step_id: int) -> EventFrame:
    step = session.get(EventFrame, step_id)
    if step is None or step.parent_id != frame.id:
        raise NotFoundError(f"no step {step_id} in this batch")
    return step


def action_label(session: Session, frame: EventFrame, action: str) -> str:
    """The button's words for an action, to repeat it after a warning."""
    verb, numbers = _parse_action(action)
    if verb == "start":
        template = _own_step_type(session, frame, numbers[0])
        element = session.get(Element, numbers[1])
        where = f" on {element.name}" if element is not None else ""
        return f"Start {template.name}{where}"
    step = _own_step(session, frame, numbers[0])
    template = session.get(EventFrameTemplate, step.event_frame_template_id)
    return f"Finish {template.name}"


def run_action(
    session: Session,
    user: CurrentUser,
    frame: EventFrame,
    action: str,
    when: datetime,
) -> tuple[str, list[str]]:
    """Start or finish one step at ``when``; the caller commits.

    Returns which happened (``started`` or ``finished``) and the warnings
    that stop the save once, apart from a time in the future, which the
    caller checks once for the whole press. Every rule is the service's:
    the overlap on the step's equipment, the window inside the batch,
    start values.
    """
    verb, numbers = _parse_action(action)
    if verb == "start":
        template = _own_step_type(session, frame, numbers[0])
        element = session.get(Element, numbers[1])
        if element is None:
            raise NotFoundError(f"no equipment {numbers[1]}")
        access.check(session, user, element, OPERATE)
        step = services.create_event_frame(
            session,
            element.id,
            template.id,
            frame.name,
            when,
            None,
            frame.id,
            started_by_id=caller_id(),
        )
        return "started", services.property_conflicts(session, step)
    step = _own_step(session, frame, numbers[0])
    if step.ended_at is not None:
        raise ValidationError("That step has already finished.")
    services.close_event_frame(
        session, step.id, when, ended_by_id=caller_id()
    )
    return "finished", []


def _open_steps(
    session: Session, frame: EventFrame, under: int | None = None
) -> list[tuple[EventFrame, int]]:
    """Every running step inside a batch, at any depth, oldest first.

    Each comes with the id of the batch's own step it sits in: itself, or
    the step that holds it.
    """
    found = []
    for child in sorted(
        services.list_event_frames(
            session, parent_id=frame.id, open_only=True
        ),
        key=lambda f: f.started_at,
    ):
        step_id = child.id if under is None else under
        found.append((child, step_id))
        found += _open_steps(session, child, step_id)
    return found


def _group(
    session: Session, frame: EventFrame, rows: list[dict], **extra
) -> dict:
    template = session.get(EventFrameTemplate, frame.event_frame_template_id)
    element = session.get(Element, frame.element_id)
    return {
        "frame_id": frame.id,
        "title": f"{template.name} on {element.name}",
        "type_name": template.name,
        "element_name": element.name,
        "measurements": rows,
        **extra,
    }


def measurement_groups(
    session: Session, user: CurrentUser, frame: EventFrame
) -> list[dict]:
    """The measurements a batch's page takes: its own, then its steps'.

    Only a running batch takes its running steps' measurements, at any
    depth. ``step_id`` says which of the batch's own steps a group goes
    under (None: the batch's own). A step with nothing measured gets no
    group. A tag shared by two of them is listed once, under the first,
    so one box never stands for two readings.
    """
    frames: list[tuple[EventFrame, int | None]] = [(frame, None)]
    if frame.ended_at is None:
        steps = _open_steps(session, frame)
        seen_frames = {
            f.id for f in access.visible(session, user, [f for f, _ in steps])
        }
        frames += [(f, under) for f, under in steps if f.id in seen_frames]
    seen: set[int] = set()
    groups = []
    for f, under in frames:
        rows = [
            m
            for m in batch_measurements(session, f)
            if m["tag_id"] is None or m["tag_id"] not in seen
        ]
        seen.update(m["tag_id"] for m in rows if m["tag_id"] is not None)
        if rows:
            groups.append(
                _group(
                    session,
                    f,
                    rows,
                    step_id=under,
                    # Its own run's line already names it.
                    headed=under is not None and under != f.id,
                )
            )
    return groups


def attach_groups(
    session: Session, rows: list[dict], groups: list[dict]
) -> None:
    """Put each run's measurements under it on the steps list.

    A running step gets its groups from the form's; a finished one gets a
    summary of its own over its window, with nothing to fill in.
    """
    for row in rows:
        for run in row["runs"]:
            if run["open"]:
                run["groups"] = [
                    g for g in groups if g["step_id"] == run["id"]
                ]
                continue
            step = session.get(EventFrame, run["id"])
            measured = batch_measurements(session, step)
            run["groups"] = (
                [_group(session, step, measured, headed=False)]
                if measured
                else []
            )


def entry_tags(
    session: Session, groups: list[dict]
) -> tuple[list[Tag], dict[int, EventFrame]]:
    """The tags a batch page's form records, and the batch each is for."""
    tags = []
    frames: dict[int, EventFrame] = {}
    for group in groups:
        frame = session.get(EventFrame, group["frame_id"])
        for m in group["measurements"]:
            if m["tag_id"] is not None:
                tags.append(session.get(Tag, m["tag_id"]))
                frames[m["tag_id"]] = frame
    return tags, frames
