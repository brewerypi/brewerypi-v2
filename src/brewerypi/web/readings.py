"""Readings from the web: see a measurement, record, correct and delete.

Recording and correcting take one press, deleting keeps its confirm page;
see ``forms`` for the pass and how warnings are accepted. Recording,
correcting and deleting are open to every role at the site, as on the MCP
operator tier, so the rules stay in ``services.tag_values`` and
``access``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from brewerypi import access, services
from brewerypi.identity import CurrentUser, caller_id
from brewerypi.models import Area, EventFrame, Site, Tag, TagValue
from brewerypi.serializers import reading_authors
from brewerypi.services import (
    NotFoundError,
    NotSelectableError,
    PermissionDeniedError,
    ServiceError,
    ValidationError,
)
from brewerypi.timezones import resolve_timezone, zone_for_tag
from brewerypi.web.auth import signed_in_user
from brewerypi.web.forms import (
    OUT_OF_DATE,
    accepted_key,
    future_warning,
    input_time,
    mint_token,
    operable,
    parse_time,
    readable,
    shown_time,
    spend_token,
    stale_page,
    unaccepted,
    utc_now,
)
from brewerypi.web.measurements import (
    batch_tag,
    measurement,
    owner,
    raw_value,
    tag_trail,
    unit_of,
    value_text,
)
from brewerypi.web.navigation import HOME, batch_trail
from brewerypi.web.rendering import page

#: Readings per page on a measurement's page.
READINGS_PAGE_SIZE = 10


def _parse_value(
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


_NOT_ON_THE_LIST = "Choose one of the options on the list."


def _message(exc: ServiceError) -> str:
    """A refusal in words for the form it came from."""
    if isinstance(exc, NotSelectableError):
        return _NOT_ON_THE_LIST
    return str(exc)


def _measurement_url(
    tag_id: int, frame: EventFrame | None = None, **extra: str
) -> str:
    """A measurement page's address, kept to a batch's readings if given."""
    params = {"batch": frame.id} if frame is not None else {}
    params.update(extra)
    query = urlencode(params)
    return f"/measurements/{tag_id}" + (f"?{query}" if query else "")


def _with_batch(path: str, frame: EventFrame | None) -> str:
    return path if frame is None else f"{path}?batch={frame.id}"


def _batch_for(
    session: Session, user: CurrentUser, tag: Tag, batch_id: int | None
) -> EventFrame | None:
    """The batch a page is seen through, or None for the whole history.

    The batch must be readable and be one whose measurements this tag
    holds; anything else is not found, as an unknown address would be.
    """
    if batch_id is None:
        return None
    frame = readable(session, user, EventFrame, batch_id)
    if not batch_tag(session, frame, tag):
        raise NotFoundError(f"tag {tag.id} is not measured by {batch_id}")
    return frame


def _tag_crumbs(
    session: Session, tag: Tag, frame: EventFrame | None
) -> list[dict]:
    if frame is not None:
        return batch_trail(session, frame)
    return tag_trail(session, tag)


def _reading_trail(
    session: Session, tag: Tag, frame: EventFrame | None
) -> list[dict]:
    """The breadcrumb for a page about one reading: down to its tag."""
    _, name = owner(session, tag)
    return [
        *_tag_crumbs(session, tag, frame),
        {"href": _measurement_url(tag.id, frame), "name": name},
    ]


def _window_warnings(frame: EventFrame | None, when) -> list[str]:
    """Say when a reading taken for a batch falls outside it."""
    if frame is None:
        return []
    if when < frame.started_at:
        return ["That time is before this batch started."]
    if frame.ended_at is not None and when > frame.ended_at:
        return ["That time is after this batch finished."]
    return []


def _title(session: Session, tag: Tag) -> str:
    """``Temperature on FV01``, or a standalone tag's own name."""
    element, name = owner(session, tag)
    return f"{name} on {element.name}" if element is not None else name


def create_router(session_factory: Callable[[], Session]) -> APIRouter:
    """Build the reading pages over the given database."""
    router = APIRouter()

    @router.get("/areas/{area_id}")
    def area_page(
        request: Request,
        area_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            area = readable(session, user, Area, area_id)
            site = session.get(Site, area.site_id)
            tags = access.visible(
                session, user, services.list_standalone_tags(session, area.id)
            )
            return page(
                request,
                "area.html",
                crumbs=[
                    HOME,
                    {"href": f"/sites/{site.id}", "name": site.name},
                ],
                area=area,
                measurements=[measurement(session, t, t.name) for t in tags],
                timezone=resolve_timezone(session, site),
            )

    def measurement_form(
        request: Request,
        session: Session,
        tag: Tag,
        page_number: int = 1,
        value: str = "",
        observed_at: str | None = None,
        error: str | None = None,
        status_code: int = 200,
        frame: EventFrame | None = None,
        warnings: list[str] = (),
    ):
        zone = zone_for_tag(session, tag)
        element, name = owner(session, tag)
        found = services.list_tag_values(
            session,
            tag.id,
            start=frame.started_at if frame is not None else None,
            end=frame.ended_at if frame is not None else None,
            limit=READINGS_PAGE_SIZE + 1,
            offset=(page_number - 1) * READINGS_PAGE_SIZE,
        )
        readings = [
            {
                "id": tv.id,
                "value": value_text(session, tag, tv),
                "at": shown_time(tv.observed_at, zone),
                **reading_authors(session, tv),
            }
            for tv in found[:READINGS_PAGE_SIZE]
        ]
        return page(
            request,
            "measurement.html",
            status_code=status_code,
            crumbs=_tag_crumbs(session, tag, frame),
            tag=tag,
            batch=frame,
            action_url=_measurement_url(tag.id, frame),
            batch_query=f"?batch={frame.id}" if frame is not None else "",
            paging_query={"batch": frame.id} if frame is not None else {},
            name=name,
            element=element,
            unit=unit_of(tag),
            options=(
                services.selectable_values(session, tag.lookup_id)
                if tag.lookup_id is not None
                else None
            ),
            value=value,
            observed_at=(
                input_time(utc_now(), zone)
                if observed_at is None
                else observed_at
            ),
            timezone=zone,
            readings=readings,
            page_number=page_number,
            has_older=len(found) > READINGS_PAGE_SIZE,
            error=error,
            warnings=list(warnings),
            accepted=accepted_key(list(warnings)),
            token=mint_token(request),
        )

    @router.get("/measurements/{tag_id}")
    def measurement_page(
        request: Request,
        tag_id: int,
        page_number: int = Query(1, alias="page", ge=1),
        batch_id: int | None = Query(None, alias="batch"),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tag = readable(session, user, Tag, tag_id)
            frame = _batch_for(session, user, tag, batch_id)
            return measurement_form(
                request, session, tag, page_number, frame=frame
            )

    @router.post("/measurements/{tag_id}")
    def record_reading(
        request: Request,
        tag_id: int,
        value: str = Form(""),
        observed_at: str = Form(""),
        accepted: str = Form(""),
        token: str = Form(""),
        batch_id: int | None = Query(None, alias="batch"),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tag = operable(session, user, Tag, tag_id)
            frame = _batch_for(session, user, tag, batch_id)

            def again(**notice):
                return measurement_form(
                    request,
                    session,
                    tag,
                    value=value,
                    observed_at=observed_at,
                    frame=frame,
                    **notice,
                )

            if not spend_token(request, token):
                return again(error=OUT_OF_DATE, status_code=409)
            zone = zone_for_tag(session, tag)
            try:
                when = parse_time(observed_at, zone, "reading's")
                number_value, option = _parse_value(session, tag, value)
                services.record_tag_value(
                    session,
                    tag.id,
                    when,
                    number_value,
                    option,
                    recorded_by_id=caller_id(),
                )
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return again(error=_message(exc), status_code=400)
            warnings = [
                *future_warning(when, "That time"),
                *_window_warnings(frame, when),
            ]
            if unaccepted(warnings, accepted):
                session.rollback()
                return again(warnings=warnings)
            session.commit()
            return RedirectResponse(
                _measurement_url(tag.id, frame, done="recorded"),
                status_code=303,
            )

    def edit_form(
        request: Request,
        session: Session,
        tv: TagValue,
        value: str | None = None,
        observed_at: str | None = None,
        error: str | None = None,
        status_code: int = 200,
        frame: EventFrame | None = None,
        warnings: list[str] = (),
    ):
        tag = session.get(Tag, tv.tag_id)
        zone = zone_for_tag(session, tag)
        return page(
            request,
            "reading_edit.html",
            status_code=status_code,
            crumbs=_reading_trail(session, tag, frame),
            reading=tv,
            action_url=_with_batch(f"/readings/{tv.id}/edit", frame),
            cancel_url=_measurement_url(tag.id, frame),
            title=_title(session, tag),
            unit=unit_of(tag),
            options=(
                services.selectable_values(session, tag.lookup_id)
                if tag.lookup_id is not None
                else None
            ),
            value=raw_value(session, tv) if value is None else value,
            observed_at=(
                input_time(tv.observed_at, zone)
                if observed_at is None
                else observed_at
            ),
            timezone=zone,
            error=error,
            warnings=list(warnings),
            accepted=accepted_key(list(warnings)),
            token=mint_token(request),
        )

    @router.get("/readings/{reading_id}/edit")
    def edit_page(
        request: Request,
        reading_id: int,
        batch_id: int | None = Query(None, alias="batch"),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tv = operable(session, user, TagValue, reading_id)
            tag = session.get(Tag, tv.tag_id)
            frame = _batch_for(session, user, tag, batch_id)
            return edit_form(request, session, tv, frame=frame)

    @router.post("/readings/{reading_id}/edit")
    def edit_reading(
        request: Request,
        reading_id: int,
        value: str = Form(""),
        observed_at: str = Form(""),
        accepted: str = Form(""),
        token: str = Form(""),
        batch_id: int | None = Query(None, alias="batch"),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tv = operable(session, user, TagValue, reading_id)
            tag = session.get(Tag, tv.tag_id)
            frame = _batch_for(session, user, tag, batch_id)

            def again(**notice):
                return edit_form(
                    request,
                    session,
                    tv,
                    value,
                    observed_at,
                    frame=frame,
                    **notice,
                )

            if not spend_token(request, token):
                return again(error=OUT_OF_DATE, status_code=409)
            zone = zone_for_tag(session, tag)
            old_raw = raw_value(session, tv)
            # Inputs are to the minute and stored times may not be, so only
            # what the person actually retyped counts as a change.
            changes: dict = {}
            moved = None
            try:
                number_value, option = _parse_value(session, tag, value)
                if value.strip() != old_raw:
                    if option is not None:
                        changes["lookup_value"] = option
                    elif number_value != tv.value:
                        changes["value"] = number_value
                if observed_at != input_time(tv.observed_at, zone):
                    moved = parse_time(observed_at, zone, "reading's")
                    changes["observed_at"] = moved.isoformat()
                if not changes:
                    raise ValidationError("Nothing has changed.")
                services.update_tag_value(
                    session,
                    tv.id,
                    updated_by_id=caller_id(),
                    **changes,
                )
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                return again(error=_message(exc), status_code=400)
            warnings = (
                [
                    *future_warning(moved, "The new time"),
                    *_window_warnings(frame, moved),
                ]
                if moved is not None
                else []
            )
            if unaccepted(warnings, accepted):
                session.rollback()
                return again(warnings=warnings)
            session.commit()
            return RedirectResponse(
                _measurement_url(tag.id, frame, done="reading_updated"),
                status_code=303,
            )

    @router.get("/readings/{reading_id}/delete")
    def delete_page(
        request: Request,
        reading_id: int,
        batch_id: int | None = Query(None, alias="batch"),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tv = operable(session, user, TagValue, reading_id)
            tag = session.get(Tag, tv.tag_id)
            frame = _batch_for(session, user, tag, batch_id)
            zone = zone_for_tag(session, tag)
            return page(
                request,
                "batch_confirm.html",
                crumbs=_reading_trail(session, tag, frame),
                heading=(
                    f"Delete the {value_text(session, tag, tv)} reading "
                    f"for {_title(session, tag)} at "
                    f"{shown_time(tv.observed_at, zone)}?"
                ),
                changes=[],
                warnings=["This can't be undone."],
                notes=[],
                action_url=_with_batch(
                    f"/readings/{reading_id}/delete", frame
                ),
                hidden={},
                token=mint_token(request),
                confirm_label="Delete reading",
                timezone=zone,
                danger=True,
                cancel_url=_measurement_url(tag.id, frame),
            )

    @router.post("/readings/{reading_id}/delete")
    def delete_reading(
        request: Request,
        reading_id: int,
        action: str = Form(""),
        token: str = Form(""),
        batch_id: int | None = Query(None, alias="batch"),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tv = operable(session, user, TagValue, reading_id)
            tag = session.get(Tag, tv.tag_id)
            frame = _batch_for(session, user, tag, batch_id)
            if action != "confirm":
                return RedirectResponse(
                    _with_batch(f"/readings/{reading_id}/delete", frame),
                    status_code=303,
                )
            if not spend_token(request, token):
                return stale_page(
                    request, _measurement_url(tag.id, frame), "measurement"
                )
            services.delete_tag_value(session, tv.id)
            session.commit()
            return RedirectResponse(
                _measurement_url(tag.id, frame, done="reading_deleted"),
                status_code=303,
            )

    return router
