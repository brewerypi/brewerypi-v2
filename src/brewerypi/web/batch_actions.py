"""Changing batches from the web: start, close, reopen, edit, link, delete.

Every write but a delete is one press. Pressing runs the real service call,
so a refusal comes back on the same form in the service's own words, and no
rule is copied here. A warning (a time in the future) stops the save once
and the form asks again; see ``forms`` for that and for the one-use pass
every form carries. Whatever a review page used to tell people before they
confirmed, such as the steps a close finishes, is on the form itself.

A delete still goes to a confirm page, because it leaves no trace.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi import access, services
from brewerypi.access import OPERATE, READ
from brewerypi.identity import CurrentUser, caller_id
from brewerypi.models import (
    Element,
    ElementTemplate,
    EventFrame,
    EventFrameAttribute,
    EventFrameTemplate,
    LookupValue,
    Tag,
    TagValue,
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
from brewerypi.web.entry import form_fields
from brewerypi.web.forms import (
    OUT_OF_DATE,
    accepted_key,
    future_warning,
    input_time,
    mint_token,
    operable,
    parse_id,
    parse_time,
    spend_token,
    stale_page,
    unaccepted,
    utc_now,
)
from brewerypi.web.measurements import property_text, value_text
from brewerypi.web.navigation import batch_trail, trail
from brewerypi.web.rendering import message_page, page

#: How many of a site's recent batches a transfer page offers as ends.
_TRANSFER_CHOICES = 200

#: A property's form field is this followed by its attribute template's id.
PROPERTY_PREFIX = "property_"


def _property_fields(
    session: Session,
    event_frame_template_id: int,
    typed: dict[int, str] | None = None,
    frame: EventFrame | None = None,
) -> list[dict]:
    """A form's property boxes: what was typed, else held, else the default.

    An option the batch holds stays offered even once it is no longer
    selectable, or the form would quietly show it blank.
    """
    held = (
        services.event_frame_property_values(session, frame.id)
        if frame is not None
        else {}
    )
    boxes = []
    for template in services.property_templates(
        session, event_frame_template_id
    ):
        options = (
            services.selectable_values(session, template.lookup_id)
            if template.lookup_id is not None
            else None
        )
        if typed is not None:
            value = typed.get(template.id, "")
        elif frame is not None:
            value = (
                property_text(held[template.name])
                if template.name in held
                else ""
            )
        else:
            value = _start_default(session, template)
        if options is not None and value and value not in options:
            options = [value, *options]
        boxes.append(
            {
                "id": template.id,
                "name": template.name,
                "required": template.is_required,
                "options": options,
                "value": value,
            }
        )
    return boxes


def _start_default(session: Session, template) -> str:
    if template.default_start_lookup_value_id is not None:
        option = session.get(
            LookupValue, template.default_start_lookup_value_id
        )
        return option.name if option is not None else ""
    if template.default_start_value is not None:
        return property_text(template.default_start_value)
    return ""


def _typed_properties(
    session: Session, event_frame_template_id: int, fields: dict[str, str]
) -> dict[int, str]:
    """What was typed into each property box, by attribute template id."""
    return {
        template.id: fields.get(f"{PROPERTY_PREFIX}{template.id}", "").strip()
        for template in services.property_templates(
            session, event_frame_template_id
        )
    }


def _conflict_warnings(session: Session, frame: EventFrame) -> list[str]:
    """Where the batches going into this one, or into its destination,
    hold different values on a list (a Lager brew and an IPA brew into one
    fermenter)."""
    warnings = services.property_conflicts(session, frame)
    if frame.destination_event_frame_id is not None:
        warnings += services.property_conflicts(
            session,
            session.get(EventFrame, frame.destination_event_frame_id),
        )
    return warnings


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


def _form_extras(request: Request, warnings: list[str]) -> dict:
    """What every write form's template needs: its warnings and a pass."""
    return {
        "warnings": warnings,
        "accepted": accepted_key(warnings),
        "token": mint_token(request),
    }


def _site_id(session: Session, element: Element) -> int:
    template = session.get(ElementTemplate, element.element_template_id)
    return template.site_id


def _open_descendants(
    session: Session, frame: EventFrame
) -> list[EventFrame]:
    """The steps inside this batch that would close along with it."""
    found = []
    for child in services.list_event_frames(session, parent_id=frame.id):
        if child.ended_at is None:
            found.append(child)
        found += _open_descendants(session, child)
    return found


def _end_values(session: Session, frames: list[EventFrame]) -> list[str]:
    """The end values closing these batches records, as people read them.

    Mirrors what the service writes: a default end value, on equipment
    whose measurement is set up.
    """
    lines = []
    for index, frame in enumerate(frames):
        for template in services.list_event_frame_attribute_templates(
            session, frame.event_frame_template_id
        ):
            if template.is_property or (
                template.default_end_value is None
                and template.default_end_lookup_value_id is None
            ):
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
                value=template.default_end_value,
                lookup_value_id=template.default_end_lookup_value_id,
            )
            text = value_text(
                session, session.get(Tag, wiring.tag_id), reading
            )
            line = f"{template.name}: {text}"
            if index:
                line += (
                    f" for {_type_name(session, frame)} on "
                    f"{_vessel_name(session, frame)}"
                )
            lines.append(line)
    return lines


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
        warnings: list[str] = (),
        typed: dict[int, str] | None = None,
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
            props=_property_fields(session, template.id, typed),
            **_form_extras(request, list(warnings)),
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
        accepted: str = Form(""),
        token: str = Form(""),
        form: dict = Depends(form_fields),
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
                "typed": _typed_properties(session, template.id, form),
            }

            def again(**notice):
                return start_form(
                    request, session, user, element, template, **fields,
                    **notice,
                )

            if not spend_token(request, token):
                return again(error=OUT_OF_DATE, status_code=409)
            zone = zone_for_element(session, element)
            try:
                start = parse_time(started_at, zone, "start")
                frame = services.create_event_frame(
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
                    properties={
                        t.name: fields["typed"][t.id]
                        for t in services.property_templates(
                            session, template.id
                        )
                    },
                )
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return again(error=str(exc), status_code=400)
            warnings = future_warning(start, "That start time")
            warnings += _conflict_warnings(session, frame)
            if unaccepted(warnings, accepted):
                session.rollback()
                return again(warnings=warnings)
            new_id = frame.id
            session.commit()
            return RedirectResponse(
                f"/batches/{new_id}?done=started", status_code=303
            )

    def close_form(
        request,
        session,
        frame,
        ended_at="",
        error=None,
        status_code=200,
        warnings=(),
    ):
        zone = zone_for_frame(session, frame)
        steps = _open_descendants(session, frame)
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
            steps=[
                f"{_type_name(session, s)} “{s.name}” on "
                f"{_vessel_name(session, s)}"
                for s in steps
            ],
            end_values=_end_values(session, [frame, *steps]),
            error=error,
            **_form_extras(request, list(warnings)),
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
        accepted: str = Form(""),
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)

            def again(**notice):
                return close_form(request, session, frame, ended_at, **notice)

            if frame.ended_at is not None:
                # Most likely a second tab: the first already closed it.
                return RedirectResponse(
                    f"/batches/{batch_id}", status_code=303
                )
            if not spend_token(request, token):
                return again(error=OUT_OF_DATE, status_code=409)
            zone = zone_for_frame(session, frame)
            try:
                end = parse_time(ended_at, zone, "end")
                services.close_event_frame(
                    session, frame.id, end, ended_by_id=caller_id()
                )
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return again(error=str(exc), status_code=400)
            warnings = future_warning(end, "That end time")
            if unaccepted(warnings, accepted):
                session.rollback()
                return again(warnings=warnings)
            session.commit()
            return RedirectResponse(
                f"/batches/{batch_id}?done=closed", status_code=303
            )

    @router.get("/batches/{batch_id}/reopen")
    def reopen_page(
        batch_id: int, user: CurrentUser = Depends(signed_in_user)
    ):
        # Reopening is a button on the batch page now; an old link lands
        # there.
        with session_factory() as session:
            operable(session, user, EventFrame, batch_id)
        return RedirectResponse(f"/batches/{batch_id}", status_code=303)

    @router.post("/batches/{batch_id}/reopen")
    def reopen_batch(
        request: Request,
        batch_id: int,
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            if frame.ended_at is None:
                return RedirectResponse(
                    f"/batches/{batch_id}", status_code=303
                )
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
        warnings=(),
        typed=None,
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
            props=_property_fields(
                session, frame.event_frame_template_id, typed, frame
            ),
            **_form_extras(request, list(warnings)),
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
        accepted: str = Form(""),
        token: str = Form(""),
        form: dict = Depends(form_fields),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            typed = _typed_properties(
                session, frame.event_frame_template_id, form
            )

            def again(**notice):
                return edit_form(
                    request,
                    session,
                    frame,
                    name,
                    started_at,
                    ended_at,
                    typed=typed,
                    **notice,
                )

            if not spend_token(request, token):
                return again(error=OUT_OF_DATE, status_code=409)
            zone = zone_for_frame(session, frame)
            # Inputs are to the minute and stored times may not be, so only
            # what the person actually retyped counts as a change.
            changes: dict = {}
            try:
                if name.strip() != frame.name:
                    changes["name"] = name
                if started_at != input_time(frame.started_at, zone):
                    changes["started_at"] = parse_time(
                        started_at, zone, "start"
                    )
                if frame.ended_at is not None and ended_at != input_time(
                    frame.ended_at, zone
                ):
                    changes["ended_at"] = parse_time(ended_at, zone, "end")
                shown = {
                    box["id"]: box["value"]
                    for box in _property_fields(
                        session, frame.event_frame_template_id, frame=frame
                    )
                }
                retyped = {
                    template.name: typed[template.id]
                    for template in services.property_templates(
                        session, frame.event_frame_template_id
                    )
                    if typed[template.id] != shown[template.id]
                }
                if not changes and not retyped:
                    raise ValidationError("Nothing has changed.")
                if changes:
                    services.update_event_frame(
                        session,
                        frame.id,
                        updated_by_id=caller_id(),
                        **changes,
                    )
                for property_name, value in retyped.items():
                    services.update_event_frame_property(
                        session,
                        frame.id,
                        property_name,
                        value,
                        updated_by_id=caller_id(),
                    )
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return again(error=str(exc), status_code=400)
            warnings: list[str] = []
            if "started_at" in changes:
                warnings += future_warning(
                    changes["started_at"], "The new start time"
                )
            if "ended_at" in changes:
                warnings += future_warning(
                    changes["ended_at"], "The new end time"
                )
            if unaccepted(warnings, accepted):
                session.rollback()
                return again(warnings=warnings)
            session.commit()
            return RedirectResponse(
                f"/batches/{batch_id}?done=updated", status_code=303
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
        warnings=(),
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
            **_form_extras(request, list(warnings)),
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
        accepted: str = Form(""),
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

            def again(**notice):
                return transfer_form(
                    request,
                    session,
                    user,
                    frame,
                    source_id,
                    destination_id,
                    **notice,
                )

            if not spend_token(request, token):
                return again(error=OUT_OF_DATE, status_code=409)
            template = session.get(
                EventFrameTemplate, frame.event_frame_template_id
            )
            implied = template.parent_flow if frame.parent_id else None
            changes: dict = {}
            if implied != "source":
                changes["source_event_frame_id"] = source
            if implied != "destination":
                changes["destination_event_frame_id"] = destination
            try:
                services.link_transfer(
                    session, frame.id, updated_by_id=caller_id(), **changes
                )
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return again(error=str(exc), status_code=400)
            warnings = _conflict_warnings(session, frame)
            if unaccepted(warnings, accepted):
                session.rollback()
                return again(warnings=warnings)
            session.commit()
            return RedirectResponse(
                f"/batches/{batch_id}?done=linked", status_code=303
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
