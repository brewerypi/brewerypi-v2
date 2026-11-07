"""Readings from the web: see a measurement, record, correct and delete.

Writes follow the batch pages' pattern (see ``batch_actions``): a form, then
a review that runs the real service call and rolls it back, then a confirm
carrying a one-use token. Recording, correcting and deleting are open to
every role at the site, as on the MCP operator tier, so the rules stay in
``services.tag_values`` and ``access``.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from brewerypi import access, services
from brewerypi.identity import CurrentUser, caller_id
from brewerypi.models import Area, Site, Tag, TagValue
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
    future_warning,
    input_time,
    mint_token,
    operable,
    parse_time,
    readable,
    shown_time,
    spend_token,
    stale_page,
    utc_now,
)
from brewerypi.web.measurements import (
    measurement,
    owner,
    raw_value,
    tag_trail,
    unit_of,
    value_text,
)
from brewerypi.web.navigation import HOME
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


def _measurement_url(tag_id: int) -> str:
    return f"/measurements/{tag_id}"


def _reading_trail(session: Session, tag: Tag) -> list[dict]:
    """The breadcrumb for a page about one reading: down to its tag."""
    _, name = owner(session, tag)
    return [
        *tag_trail(session, tag),
        {"href": _measurement_url(tag.id), "name": name},
    ]


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
    ):
        zone = zone_for_tag(session, tag)
        element, name = owner(session, tag)
        found = services.list_tag_values(
            session,
            tag.id,
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
            crumbs=tag_trail(session, tag),
            tag=tag,
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
        )

    @router.get("/measurements/{tag_id}")
    def measurement_page(
        request: Request,
        tag_id: int,
        page_number: int = Query(1, alias="page", ge=1),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tag = readable(session, user, Tag, tag_id)
            return measurement_form(request, session, tag, page_number)

    @router.post("/measurements/{tag_id}")
    def record_reading(
        request: Request,
        tag_id: int,
        value: str = Form(""),
        observed_at: str = Form(""),
        action: str = Form("review"),
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tag = operable(session, user, Tag, tag_id)
            back = _measurement_url(tag.id)
            if action == "change":
                return measurement_form(
                    request, session, tag, value=value, observed_at=observed_at
                )
            if action == "confirm" and not spend_token(request, token):
                return stale_page(request, back, "measurement")
            zone = zone_for_tag(session, tag)
            try:
                when = parse_time(observed_at, zone, "reading's")
                number_value, option = _parse_value(session, tag, value)
                reading = services.record_tag_value(
                    session,
                    tag.id,
                    when,
                    number_value,
                    option,
                    recorded_by_id=caller_id(),
                )
                text = value_text(session, tag, reading)
                if action == "confirm":
                    session.commit()
                    return RedirectResponse(
                        f"{back}?done=recorded", status_code=303
                    )
                session.rollback()
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                message = (
                    _NOT_ON_THE_LIST
                    if isinstance(exc, NotSelectableError)
                    else str(exc)
                )
                return measurement_form(
                    request,
                    session,
                    tag,
                    value=value,
                    observed_at=observed_at,
                    error=message,
                    status_code=400,
                )
            return page(
                request,
                "batch_confirm.html",
                crumbs=tag_trail(session, tag),
                heading=(
                    f"Record {text} for {_title(session, tag)} at "
                    f"{shown_time(when, zone)}?"
                ),
                changes=[],
                warnings=future_warning(when, "That time"),
                notes=[],
                action_url=back,
                hidden={"value": value, "observed_at": observed_at},
                token=mint_token(request),
                confirm_label="Record reading",
                timezone=zone,
                can_change=True,
                cancel_url=back,
            )

    def edit_form(
        request: Request,
        session: Session,
        tv: TagValue,
        value: str | None = None,
        observed_at: str | None = None,
        error: str | None = None,
        status_code: int = 200,
    ):
        tag = session.get(Tag, tv.tag_id)
        zone = zone_for_tag(session, tag)
        return page(
            request,
            "reading_edit.html",
            status_code=status_code,
            crumbs=_reading_trail(session, tag),
            reading=tv,
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
        )

    @router.get("/readings/{reading_id}/edit")
    def edit_page(
        request: Request,
        reading_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tv = operable(session, user, TagValue, reading_id)
            return edit_form(request, session, tv)

    @router.post("/readings/{reading_id}/edit")
    def edit_reading(
        request: Request,
        reading_id: int,
        value: str = Form(""),
        observed_at: str = Form(""),
        action: str = Form("review"),
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tv = operable(session, user, TagValue, reading_id)
            tag = session.get(Tag, tv.tag_id)
            back = _measurement_url(tag.id)
            if action == "change":
                return edit_form(request, session, tv, value, observed_at)
            if action == "confirm" and not spend_token(request, token):
                return stale_page(request, back, "measurement")
            zone = zone_for_tag(session, tag)
            old_raw = raw_value(session, tv)
            old_text = value_text(session, tag, tv)
            old_time = tv.observed_at
            # Inputs are to the minute and stored times may not be, so only
            # what the person actually retyped counts as a change.
            changes: dict = {}
            lines: list[str] = []
            moved = None
            try:
                number_value, option = _parse_value(session, tag, value)
                if value.strip() != old_raw:
                    if option is not None:
                        changes["lookup_value"] = option
                    elif number_value != tv.value:
                        changes["value"] = number_value
                if observed_at != input_time(old_time, zone):
                    moved = parse_time(observed_at, zone, "reading's")
                    changes["observed_at"] = moved.isoformat()
                if not changes:
                    raise ValidationError("Nothing has changed.")
                updated = services.update_tag_value(
                    session,
                    tv.id,
                    updated_by_id=caller_id(),
                    **changes,
                )
                if "value" in changes or "lookup_value" in changes:
                    lines.append(
                        f"Value: {old_text} to "
                        f"{value_text(session, tag, updated)}"
                    )
                if moved is not None:
                    lines.append(
                        f"Time: {shown_time(old_time, zone)} to "
                        f"{shown_time(moved, zone)}"
                    )
                if action == "confirm":
                    session.commit()
                    return RedirectResponse(
                        f"{back}?done=reading_updated", status_code=303
                    )
                session.rollback()
            except ServiceError as exc:
                session.rollback()
                if isinstance(exc, (NotFoundError, PermissionDeniedError)):
                    raise
                message = (
                    _NOT_ON_THE_LIST
                    if isinstance(exc, NotSelectableError)
                    else str(exc)
                )
                return edit_form(
                    request, session, tv, value, observed_at, message, 400
                )
            return page(
                request,
                "batch_confirm.html",
                crumbs=_reading_trail(session, tag),
                heading=f"Correct this {_title(session, tag)} reading?",
                changes=lines,
                warnings=(
                    future_warning(moved, "The new time")
                    if moved is not None
                    else []
                ),
                notes=[],
                action_url=f"/readings/{reading_id}/edit",
                hidden={"value": value, "observed_at": observed_at},
                token=mint_token(request),
                confirm_label="Save changes",
                timezone=zone,
                can_change=True,
                cancel_url=back,
            )

    @router.get("/readings/{reading_id}/delete")
    def delete_page(
        request: Request,
        reading_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tv = operable(session, user, TagValue, reading_id)
            tag = session.get(Tag, tv.tag_id)
            zone = zone_for_tag(session, tag)
            return page(
                request,
                "batch_confirm.html",
                crumbs=_reading_trail(session, tag),
                heading=(
                    f"Delete the {value_text(session, tag, tv)} reading "
                    f"for {_title(session, tag)} at "
                    f"{shown_time(tv.observed_at, zone)}?"
                ),
                changes=[],
                warnings=["This can't be undone."],
                notes=[],
                action_url=f"/readings/{reading_id}/delete",
                hidden={},
                token=mint_token(request),
                confirm_label="Delete reading",
                timezone=zone,
                danger=True,
                can_change=False,
                cancel_url=_measurement_url(tag.id),
            )

    @router.post("/readings/{reading_id}/delete")
    def delete_reading(
        request: Request,
        reading_id: int,
        action: str = Form(""),
        token: str = Form(""),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            tv = operable(session, user, TagValue, reading_id)
            back = _measurement_url(tv.tag_id)
            if action != "confirm":
                return RedirectResponse(
                    f"/readings/{reading_id}/delete", status_code=303
                )
            if not spend_token(request, token):
                return stale_page(request, back, "measurement")
            services.delete_tag_value(session, tv.id)
            session.commit()
            return RedirectResponse(
                f"{back}?done=reading_deleted", status_code=303
            )

    return router
