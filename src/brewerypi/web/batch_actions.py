"""Changing batches from the web: start, close, reopen, edit, link, delete.

Every write goes through the same three steps. A form (or, for a write with
nothing to fill in, nothing) leads to a review page, and only confirming that
page writes anything.

The review runs the real service call and rolls it back, so a refusal appears
before the person confirms, in the service's own words, and no rule is copied
here. The confirm button carries a token kept in the session and spent on use,
which stops both a double tap and another site submitting the form. The token
exists only on a review page, so no write can skip the review.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from brewerypi import access, services
from brewerypi.access import OPERATE, READ
from brewerypi.identity import CurrentUser, caller_id
from brewerypi.models import (
    Element,
    ElementTemplate,
    EventFrame,
    EventFrameTemplate,
)
from brewerypi.services import (
    NotFoundError,
    PermissionDeniedError,
    ServiceError,
    ValidationError,
)
from brewerypi.timezones import (
    zone_for_element,
    zone_for_frame,
)
from brewerypi.web.auth import signed_in_user
from brewerypi.web.forms import (
    future_warning,
    input_time,
    mint_token,
    operable,
    parse_id,
    parse_time,
    shown_time,
    spend_token,
    stale_page,
    utc_now,
)
from brewerypi.web.navigation import batch_trail, trail
from brewerypi.web.rendering import message_page, page

#: How many of a site's recent batches a transfer page offers as ends.
_TRANSFER_CHOICES = 200


def _optional_frame(
    session: Session, frame_id: int | None
) -> EventFrame | None:
    return session.get(EventFrame, frame_id) if frame_id else None


def _type_name(session: Session, frame: EventFrame) -> str:
    template = session.get(EventFrameTemplate, frame.event_frame_template_id)
    return template.name if template is not None else "Batch"


def _vessel_name(session: Session, frame: EventFrame) -> str:
    element = session.get(Element, frame.element_id)
    return element.name if element is not None else "unknown equipment"


def _choice(session: Session, frame: EventFrame) -> dict:
    """A batch as one option in a picker, named the way people know it."""
    return {
        "id": frame.id,
        "label": (
            f"{_vessel_name(session, frame)}: {frame.name} "
            f"({_type_name(session, frame)})"
        ),
    }


def _transfer_lines(session: Session, frame: EventFrame) -> list[str]:
    """What a review page says about a batch's two transfer ends."""

    def end(label: str, frame_id: int | None) -> str:
        row = _optional_frame(session, frame_id)
        return f"{label}: " + (
            _choice(session, row)["label"] if row else "nothing recorded"
        )

    return [
        end("Source", frame.source_event_frame_id),
        end("Destination", frame.destination_event_frame_id),
    ]


def _site_id(session: Session, element: Element) -> int:
    template = session.get(ElementTemplate, element.element_template_id)
    return template.site_id


def _open_descendants(session: Session, frame: EventFrame) -> int:
    """How many steps inside this batch would close along with it."""
    count = 0
    for child in services.list_event_frames(session, parent_id=frame.id):
        count += child.ended_at is None
        count += _open_descendants(session, child)
    return count


def _all_descendants(session: Session, frame: EventFrame) -> int:
    children = services.list_event_frames(session, parent_id=frame.id)
    return len(children) + sum(_all_descendants(session, c) for c in children)


def _transfer_choices(
    session: Session, user: CurrentUser, frame: EventFrame
) -> list[dict]:
    """Batches this one could flow between: running throughout its window.

    The service is what enforces this. Filtering here only keeps choices
    that would be refused off the list.
    """
    element = session.get(Element, frame.element_id)
    candidates = services.list_event_frames(
        session,
        site_id=_site_id(session, element),
        limit=_TRANSFER_CHOICES,
    )
    keep = []
    for other in access.visible(session, user, candidates):
        if other.id == frame.id or other.started_at > frame.started_at:
            continue
        if other.ended_at is not None and (
            frame.ended_at is None or other.ended_at < frame.ended_at
        ):
            continue
        keep.append(_choice(session, other))
    return keep


def create_router(session_factory: Callable[[], Session]) -> APIRouter:
    """Build the batch write pages over the given database."""
    router = APIRouter()

    def start_form(
        request: Request,
        session: Session,
        user: CurrentUser,
        element: Element,
        template: EventFrameTemplate,
        name: str = "",
        started_at: str = "",
        parent_id: str = "",
        source_id: str = "",
        destination_id: str = "",
        error: str | None = None,
        status_code: int = 200,
    ):
        zone = zone_for_element(session, element)
        parents = []
        if template.parent_id is not None:
            parents = [
                _choice(session, f)
                for f in access.visible(
                    session,
                    user,
                    services.list_event_frames(
                        session,
                        event_frame_template_id=template.parent_id,
                        open_only=True,
                        element_id=element.parent_id,
                    ),
                )
            ]
        site_batches = access.visible(
            session,
            user,
            services.list_event_frames(
                session,
                site_id=_site_id(session, element),
                open_only=True,
            ),
        )
        ends = [_choice(session, f) for f in site_batches]
        return page(
            request,
            "batch_start.html",
            crumbs=trail(session, element),
            status_code=status_code,
            element=element,
            template=template,
            timezone=zone,
            error=error,
            name=name,
            started_at=started_at or input_time(utc_now(), zone),
            parents=parents,
            # With one running batch to belong to, there's nothing to ask.
            parent_id=parent_id
            or (str(parents[0]["id"]) if len(parents) == 1 else ""),
            ends=ends,
            source_id=source_id,
            destination_id=destination_id,
        )

    @router.get("/equipment/{element_id}/start")
    def start_page(
        request: Request,
        element_id: int,
        type_id: str = "",
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            element = operable(session, user, Element, element_id)
            chosen = parse_id(type_id)
            if chosen is None:
                raise NotFoundError("no batch type chosen")
            template = session.get(EventFrameTemplate, chosen)
            if template is None:
                raise NotFoundError(f"no batch type {chosen}")
            access.check(session, user, template, READ)
            return start_form(request, session, user, element, template)

    @router.post("/equipment/{element_id}/start")
    def start_batch(
        request: Request,
        element_id: int,
        type_id: str = Form(""),
        name: str = Form(""),
        started_at: str = Form(""),
        parent_id: str = Form(""),
        source_id: str = Form(""),
        destination_id: str = Form(""),
        action: str = Form("review"),
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            element = operable(session, user, Element, element_id)
            chosen = parse_id(type_id)
            if chosen is None:
                raise NotFoundError("no batch type chosen")
            template = session.get(EventFrameTemplate, chosen)
            if template is None:
                raise NotFoundError(f"no batch type {chosen}")
            access.check(session, user, template, READ)
            parent = parse_id(parent_id)
            source = parse_id(source_id)
            destination = parse_id(destination_id)
            access.check_rows(
                session,
                user,
                [
                    (EventFrame, parent, OPERATE),
                    (EventFrame, source, READ),
                    (EventFrame, destination, READ),
                ],
            )
            fields = {
                "name": name,
                "started_at": started_at,
                "parent_id": parent_id,
                "source_id": source_id,
                "destination_id": destination_id,
            }
            if action == "change":
                return start_form(
                    request, session, user, element, template, **fields
                )
            zone = zone_for_element(session, element)
            if action == "confirm" and not spend_token(request, token):
                return stale_page(request, f"/equipment/{element_id}")
            try:
                start = parse_time(started_at, zone, "start")

                def write() -> EventFrame:
                    return services.create_event_frame(
                        session,
                        element.id,
                        template.id,
                        name,
                        start,
                        None,
                        parent,
                        source,
                        destination,
                        started_by_id=caller_id(),
                    )

                frame = write()
                if action == "confirm":
                    new_id = frame.id
                    session.commit()
                    return RedirectResponse(
                        f"/batches/{new_id}?done=started", status_code=303
                    )
                clean_name = frame.name
                # Read before the rollback, which takes the new row away.
                # An implied end counts: the review should show it too.
                transfer = (
                    _transfer_lines(session, frame)
                    if frame.source_event_frame_id is not None
                    or frame.destination_event_frame_id is not None
                    else []
                )
                session.rollback()
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return start_form(
                    request,
                    session,
                    user,
                    element,
                    template,
                    error=str(exc),
                    status_code=400,
                    **fields,
                )
            return page(
                request,
                "batch_confirm.html",
                crumbs=trail(session, element),
                heading=(
                    f"Start {template.name} “{clean_name}” on "
                    f"{element.name} at {shown_time(start, zone)}?"
                ),
                changes=transfer,
                warnings=future_warning(start, "That start time"),
                notes=[],
                action_url=f"/equipment/{element_id}/start",
                hidden={"type_id": type_id, **fields},
                token=mint_token(request),
                confirm_label="Start batch",
                can_change=True,
                cancel_url=f"/equipment/{element_id}",
                timezone=zone,
            )

    def close_form(
        request,
        session,
        frame,
        ended_at="",
        error=None,
        status_code=200,
    ):
        zone = zone_for_frame(session, frame)
        return page(
            request,
            "batch_close.html",
            crumbs=batch_trail(session, frame),
            status_code=status_code,
            frame=frame,
            type_name=_type_name(session, frame),
            vessel=_vessel_name(session, frame),
            timezone=zone,
            ended_at=ended_at or input_time(utc_now(), zone),
            steps=_open_descendants(session, frame),
            error=error,
        )

    @router.get("/batches/{batch_id}/close")
    def close_page(
        request: Request,
        batch_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            if frame.ended_at is not None:
                return RedirectResponse(
                    f"/batches/{batch_id}", status_code=303
                )
            return close_form(request, session, frame)

    @router.post("/batches/{batch_id}/close")
    def close_batch(
        request: Request,
        batch_id: int,
        ended_at: str = Form(""),
        action: str = Form("review"),
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            if action == "change":
                return close_form(request, session, frame, ended_at)
            if action == "confirm" and not spend_token(request, token):
                return stale_page(request, f"/batches/{batch_id}")
            zone = zone_for_frame(session, frame)
            steps = _open_descendants(session, frame)
            heading = (
                f"Close {_type_name(session, frame)} “{frame.name}” "
                f"on {_vessel_name(session, frame)}"
            )
            try:
                end = parse_time(ended_at, zone, "end")
                services.close_event_frame(
                    session, frame.id, end, ended_by_id=caller_id()
                )
                if action == "confirm":
                    session.commit()
                    return RedirectResponse(
                        f"/batches/{batch_id}?done=closed", status_code=303
                    )
                session.rollback()
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return close_form(
                    request, session, frame, ended_at, str(exc), 400
                )
            notes = ["Any default end values will be recorded at that time."]
            if steps:
                notes.insert(
                    0,
                    f"This also closes {steps} running "
                    f"step{'' if steps == 1 else 's'} inside it.",
                )
            return page(
                request,
                "batch_confirm.html",
                crumbs=batch_trail(session, frame),
                heading=f"{heading} at {shown_time(end, zone)}?",
                warnings=future_warning(end, "That end time"),
                notes=notes,
                action_url=f"/batches/{batch_id}/close",
                hidden={"ended_at": ended_at},
                token=mint_token(request),
                confirm_label="Close batch",
                can_change=True,
                cancel_url=f"/batches/{batch_id}",
                timezone=zone,
            )

    @router.get("/batches/{batch_id}/reopen")
    def reopen_page(
        request: Request,
        batch_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            if frame.ended_at is None:
                return RedirectResponse(
                    f"/batches/{batch_id}", status_code=303
                )
            zone = zone_for_frame(session, frame)
            name = frame.name
            type_name = _type_name(session, frame)
            vessel = _vessel_name(session, frame)
            try:
                services.reopen_event_frame(
                    session, frame.id, updated_by_id=caller_id()
                )
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return message_page(
                    request,
                    f"Can't reopen “{name}”",
                    str(exc),
                    status_code=409,
                    tone="bad",
                    link=f"/batches/{batch_id}",
                    link_text="Back to the batch",
                )
            session.rollback()
            return page(
                request,
                "batch_confirm.html",
                crumbs=batch_trail(session, frame),
                heading=(
                    f"Reopen {type_name} “{name}” on {vessel}?"
                ),
                warnings=[],
                notes=[
                    "Use this if it was closed by mistake. If only the end "
                    "time is wrong, edit the batch instead.",
                    "Readings recorded when it closed stay where they are.",
                ],
                action_url=f"/batches/{batch_id}/reopen",
                hidden={},
                token=mint_token(request),
                confirm_label="Reopen batch",
                can_change=False,
                cancel_url=f"/batches/{batch_id}",
                timezone=zone,
            )

    @router.post("/batches/{batch_id}/reopen")
    def reopen_batch(
        request: Request,
        batch_id: int,
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            if not spend_token(request, token):
                return stale_page(request, f"/batches/{batch_id}")
            try:
                services.reopen_event_frame(
                    session, frame.id, updated_by_id=caller_id()
                )
                session.commit()
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return message_page(
                    request,
                    "Can't reopen that batch",
                    str(exc),
                    status_code=409,
                    tone="bad",
                    link=f"/batches/{batch_id}",
                    link_text="Back to the batch",
                )
            return RedirectResponse(
                f"/batches/{batch_id}?done=reopened", status_code=303
            )

    def edit_form(
        request,
        session,
        frame,
        name=None,
        started_at=None,
        ended_at=None,
        error=None,
        status_code=200,
    ):
        zone = zone_for_frame(session, frame)
        return page(
            request,
            "batch_edit.html",
            crumbs=batch_trail(session, frame),
            status_code=status_code,
            frame=frame,
            type_name=_type_name(session, frame),
            vessel=_vessel_name(session, frame),
            timezone=zone,
            name=frame.name if name is None else name,
            started_at=(
                input_time(frame.started_at, zone)
                if started_at is None
                else started_at
            ),
            ended_at=(
                ended_at
                if ended_at is not None
                else (
                    input_time(frame.ended_at, zone)
                    if frame.ended_at is not None
                    else ""
                )
            ),
            error=error,
        )

    @router.get("/batches/{batch_id}/edit")
    def edit_page(
        request: Request,
        batch_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            return edit_form(request, session, frame)

    @router.post("/batches/{batch_id}/edit")
    def edit_batch(
        request: Request,
        batch_id: int,
        name: str = Form(""),
        started_at: str = Form(""),
        ended_at: str = Form(""),
        action: str = Form("review"),
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            if action == "change":
                return edit_form(
                    request, session, frame, name, started_at, ended_at
                )
            if action == "confirm" and not spend_token(request, token):
                return stale_page(request, f"/batches/{batch_id}")
            zone = zone_for_frame(session, frame)
            old_name = frame.name
            old_start = frame.started_at
            old_end = frame.ended_at
            # Inputs are to the minute and stored times may not be, so only
            # what the person actually retyped counts as a change.
            changes: dict = {}
            changed: list[str] = []
            try:
                if name.strip() != old_name:
                    changes["name"] = name
                if started_at != input_time(old_start, zone):
                    moved = parse_time(started_at, zone, "start")
                    changes["started_at"] = moved
                    changed.append(
                        f"Start: {shown_time(old_start, zone)} to "
                        f"{shown_time(moved, zone)}"
                    )
                if old_end is not None and ended_at != input_time(
                    old_end, zone
                ):
                    moved = parse_time(ended_at, zone, "end")
                    changes["ended_at"] = moved
                    changed.append(
                        f"End: {shown_time(old_end, zone)} to "
                        f"{shown_time(moved, zone)}"
                    )
                if not changes:
                    raise ValidationError("Nothing has changed.")
                updated = services.update_event_frame(
                    session,
                    frame.id,
                    updated_by_id=caller_id(),
                    **changes,
                )
                if "name" in changes:
                    changed.insert(
                        0,
                        f"Name: “{old_name}” to "
                        f"“{updated.name}”",
                    )
                if action == "confirm":
                    session.commit()
                    return RedirectResponse(
                        f"/batches/{batch_id}?done=updated", status_code=303
                    )
                session.rollback()
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return edit_form(
                    request,
                    session,
                    frame,
                    name,
                    started_at,
                    ended_at,
                    str(exc),
                    400,
                )
            notes = ["Readings already recorded stay where they are."]
            warnings: list[str] = []
            if "started_at" in changes:
                warnings += future_warning(
                    changes["started_at"], "The new start time"
                )
            if "ended_at" in changes:
                warnings += future_warning(
                    changes["ended_at"], "The new end time"
                )
            return page(
                request,
                "batch_confirm.html",
                crumbs=batch_trail(session, frame),
                heading=f"Save these changes to “{old_name}”?",
                changes=changed,
                warnings=warnings,
                notes=notes,
                action_url=f"/batches/{batch_id}/edit",
                hidden={
                    "name": name,
                    "started_at": started_at,
                    "ended_at": ended_at,
                },
                token=mint_token(request),
                confirm_label="Save changes",
                can_change=True,
                cancel_url=f"/batches/{batch_id}",
                timezone=zone,
            )

    def transfer_form(
        request,
        session,
        user,
        frame,
        source_id=None,
        destination_id=None,
        error=None,
        status_code=200,
    ):
        template = session.get(
            EventFrameTemplate, frame.event_frame_template_id
        )
        implied = template.parent_flow if frame.parent_id else None
        choices = _transfer_choices(session, user, frame)
        # What is already recorded stays offered even if it no longer fits,
        # or the form would silently clear it.
        for current in (
            frame.source_event_frame_id,
            frame.destination_event_frame_id,
        ):
            if current and all(c["id"] != current for c in choices):
                row = session.get(EventFrame, current)
                if access.visible(session, user, [row]):
                    choices.append(_choice(session, row))
        parent = _optional_frame(session, frame.parent_id)
        return page(
            request,
            "batch_transfer.html",
            crumbs=batch_trail(session, frame),
            status_code=status_code,
            frame=frame,
            type_name=_type_name(session, frame),
            vessel=_vessel_name(session, frame),
            implied=implied,
            parent_label=(
                _choice(session, parent)["label"] if parent else None
            ),
            choices=choices,
            source_id=(
                str(frame.source_event_frame_id or "")
                if source_id is None
                else source_id
            ),
            destination_id=(
                str(frame.destination_event_frame_id or "")
                if destination_id is None
                else destination_id
            ),
            error=error,
        )

    @router.get("/batches/{batch_id}/transfer")
    def transfer_page(
        request: Request,
        batch_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            return transfer_form(request, session, user, frame)

    @router.post("/batches/{batch_id}/transfer")
    def transfer_batch(
        request: Request,
        batch_id: int,
        source_id: str = Form(""),
        destination_id: str = Form(""),
        action: str = Form("review"),
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            source = parse_id(source_id)
            destination = parse_id(destination_id)
            access.check_rows(
                session,
                user,
                [
                    (EventFrame, source, READ),
                    (EventFrame, destination, READ),
                ],
            )
            if action == "change":
                return transfer_form(
                    request, session, user, frame, source_id, destination_id
                )
            if action == "confirm" and not spend_token(request, token):
                return stale_page(request, f"/batches/{batch_id}")
            template = session.get(
                EventFrameTemplate, frame.event_frame_template_id
            )
            implied = template.parent_flow if frame.parent_id else None
            changes: dict = {}
            if implied != "source":
                changes["source_event_frame_id"] = source
            if implied != "destination":
                changes["destination_event_frame_id"] = destination
            name = frame.name
            try:
                updated = services.link_transfer(
                    session, frame.id, updated_by_id=caller_id(), **changes
                )
                lines = _transfer_lines(session, updated)
                if action == "confirm":
                    session.commit()
                    return RedirectResponse(
                        f"/batches/{batch_id}?done=linked", status_code=303
                    )
                session.rollback()
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return transfer_form(
                    request,
                    session,
                    user,
                    frame,
                    source_id,
                    destination_id,
                    str(exc),
                    400,
                )
            return page(
                request,
                "batch_confirm.html",
                crumbs=batch_trail(session, frame),
                heading=f"Save the transfer for “{name}”?",
                changes=lines,
                warnings=[],
                notes=[],
                action_url=f"/batches/{batch_id}/transfer",
                hidden={
                    "source_id": source_id,
                    "destination_id": destination_id,
                },
                token=mint_token(request),
                confirm_label="Save transfer",
                can_change=True,
                cancel_url=f"/batches/{batch_id}",
                timezone=zone_for_frame(session, frame),
            )

    @router.get("/batches/{batch_id}/delete")
    def delete_page(
        request: Request,
        batch_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            blockers = access.visible(
                session,
                user,
                services.transfers_blocking_delete(session, frame.id),
            )
            if blockers:
                return page(
                    request,
                    "batch_delete_blocked.html",
                    crumbs=batch_trail(session, frame),
                    status_code=409,
                    frame=frame,
                    blockers=[_choice(session, b) for b in blockers],
                )
            nested = _all_descendants(session, frame)
            notes = ["Readings and measurements are kept."]
            if nested:
                notes.insert(
                    0,
                    f"This also deletes {nested} "
                    f"step{'' if nested == 1 else 's'} inside it.",
                )
            return page(
                request,
                "batch_confirm.html",
                crumbs=batch_trail(session, frame),
                heading=(
                    f"Delete {_type_name(session, frame)} "
                    f"“{frame.name}” on "
                    f"{_vessel_name(session, frame)}?"
                ),
                warnings=["This can't be undone."],
                notes=notes,
                action_url=f"/batches/{batch_id}/delete",
                hidden={},
                token=mint_token(request),
                confirm_label="Delete batch",
                danger=True,
                can_change=False,
                cancel_url=f"/batches/{batch_id}",
                timezone=zone_for_frame(session, frame),
            )

    @router.post("/batches/{batch_id}/delete")
    def delete_batch(
        request: Request,
        batch_id: int,
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            if not spend_token(request, token):
                return stale_page(request, f"/batches/{batch_id}")
            element_id = frame.element_id
            try:
                services.delete_event_frame(session, frame.id)
                session.commit()
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return message_page(
                    request,
                    "Can't delete that batch",
                    str(exc),
                    status_code=409,
                    tone="bad",
                    link=f"/batches/{batch_id}",
                    link_text="Back to the batch",
                )
            return RedirectResponse(
                f"/equipment/{element_id}?done=deleted", status_code=303
            )

    return router
