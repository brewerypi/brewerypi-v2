"""Viewing batches: what is running, and what has run.

Changing them lives in ``batch_actions``. Entering several measurements
at once is here, because a refused save brings back these same pages (see
``entry``).

Every page checks the row its address names with ``access.check``, and every
list keeps only what the signed-in user may read, so another company's ids
look exactly like ids that don't exist.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi import access, services
from brewerypi.identity import CurrentUser
from brewerypi.models import (
    Element,
    ElementTemplate,
    Enterprise,
    EventFrame,
    EventFrameTemplate,
    Site,
    Tag,
)
from brewerypi.serializers import event_frame_dict
from brewerypi.services import (
    NotFoundError,
    PermissionDeniedError,
    ServiceError,
)
from brewerypi.timezones import (
    resolve_timezone,
    zone_for_element,
    zone_for_frame,
)
from brewerypi.web.auth import signed_in_user
from brewerypi.web.entry import (
    entry_form,
    form_fields,
    record_entries,
    typed_values,
)
from brewerypi.web.forms import (
    OUT_OF_DATE,
    future_warning,
    mint_token,
    operable,
    parse_time,
    readable,
    spend_token,
    unaccepted,
)
from brewerypi.web.measurements import (
    areas_with_measurements,
    batch_properties,
    element_measurements,
    property_text,
)
from brewerypi.web.navigation import HOME, batch_page_trail, trail
from brewerypi.web.rendering import elapsed, page
from brewerypi.web.steps import (
    action_label,
    attach_groups,
    entry_tags,
    measurement_groups,
    run_action,
    step_rows,
)

#: How many finished batches a page lists before offering older ones.
PAGE_SIZE = 25


def _batch(session: Session, frame: EventFrame) -> dict:
    """A batch as the pages show it: the serialized row plus its context."""
    batch_type = session.get(EventFrameTemplate, frame.event_frame_template_id)
    until = frame.ended_at or datetime.now(timezone.utc).replace(tzinfo=None)
    row = event_frame_dict(session, frame)
    return {
        **row,
        "type_name": batch_type.name if batch_type is not None else "Batch",
        "elapsed": elapsed(frame.started_at, until),
        "labels": [property_text(v) for v in row["properties"].values()],
    }


def _nested(batches: list[dict]) -> list[dict]:
    """Put each batch under its parent, oldest step first.

    A batch whose parent isn't in the list stays at the top: the parent is
    out of the user's sight, or this list simply doesn't include it.
    """
    by_id = {b["id"]: {**b, "steps": []} for b in batches}
    roots = []
    for batch in by_id.values():
        parent = by_id.get(batch["parent_id"])
        if parent is not None:
            parent["steps"].append(batch)
        else:
            roots.append(batch)
    for batch in by_id.values():
        batch["steps"].sort(key=lambda b: b["started_at"])
    return sorted(roots, key=lambda b: b["element_name"] or "")


def _finished(
    session: Session, user: CurrentUser, page_number: int, **filters
) -> tuple[list[dict], bool]:
    """One page of finished batches, newest first, and whether more remain."""
    frames = services.list_event_frames(
        session,
        closed_only=True,
        limit=PAGE_SIZE + 1,
        offset=(page_number - 1) * PAGE_SIZE,
        **filters,
    )
    has_older = len(frames) > PAGE_SIZE
    shown = access.visible(session, user, frames[:PAGE_SIZE])
    return [_batch(session, f) for f in shown], has_older


def _type_filter(
    site_id: int,
    here: dict,
    key: str,
    counts: dict[str, int],
    selected: str | None,
) -> list[dict]:
    """The buttons that filter one of the site page's lists by batch type.

    ``key`` is the list's address parameter (``running`` or ``finished``),
    ``here`` every parameter of the current address, so a button keeps the
    other list's choice. Choosing a finished type starts its paging over.
    Empty when there is nothing to choose between.
    """
    if len(counts) < 2 and selected is None:
        return []

    def href(value: str | None) -> str:
        params = {k: v for k, v in here.items() if k != key}
        if key == "finished":
            params.pop("page", None)
        if value is not None:
            params[key] = value
        query = urlencode(params)
        return f"/sites/{site_id}" + (f"?{query}" if query else "")

    buttons = [
        {
            "name": "All",
            "count": sum(counts.values()),
            "href": href(None),
            "on": selected is None,
        }
    ]
    for name in sorted(counts):
        buttons.append(
            {
                "name": name,
                "count": counts[name],
                "href": href(name),
                "on": name == selected,
            }
        )
    return buttons


def _count_by_name(counts: dict[int, int], names: dict[int, str]):
    """Fold per-template counts into per-name ones."""
    by_name: dict[str, int] = {}
    for template_id, count in counts.items():
        name = names[template_id]
        by_name[name] = by_name.get(name, 0) + count
    return by_name


def _busy(elements: list[Element], running_ids: set[int]) -> set[int]:
    """Equipment that is running a batch, or holds equipment that is."""
    parent_of = {e.id: e.parent_id for e in elements}
    busy: set[int] = set()
    for element_id in running_ids:
        while element_id is not None and element_id not in busy:
            busy.add(element_id)
            element_id = parent_of.get(element_id)
    return busy


def _site_busy(session: Session, site_id: int) -> set[int]:
    """Every busy element at a site, by the rule in :func:`_busy`.

    Uses every element and open batch at the site, seen or not: whether a
    vessel is free doesn't depend on who is looking.
    """
    running_ids = {
        f.element_id
        for f in services.list_event_frames(
            session, site_id=site_id, open_only=True
        )
    }
    return _busy(services.list_elements(session, site_id=site_id), running_ids)


def _chips(elements: list[Element], busy: set[int]) -> list[dict]:
    return [
        {"id": e.id, "name": e.name, "busy": e.id in busy} for e in elements
    ]


def _equipment_by_kind(
    session: Session, user: CurrentUser, site: Site
) -> list[dict]:
    """The site's top-level equipment, grouped by kind, saying what is busy.

    Equipment inside other equipment is reached from its parent's page.
    """
    busy = _site_busy(session, site.id)
    elements = [
        e
        for e in access.visible(
            session, user, services.list_elements(session, site_id=site.id)
        )
        if e.parent_id is None
    ]
    kinds = []
    for kind in services.list_element_templates(session, site.id):
        members = _chips(
            [e for e in elements if e.element_template_id == kind.id], busy
        )
        if members:
            kinds.append({"name": kind.name, "equipment": members})
    return kinds


def create_router(session_factory: Callable[[], Session]) -> APIRouter:
    """Build the batch pages over the given database."""
    router = APIRouter()

    @router.get("/")
    def home(
        request: Request, user: CurrentUser = Depends(signed_in_user)
    ):
        with session_factory() as session:
            enterprises = access.visible(
                session,
                user,
                session.scalars(select(Enterprise).order_by(Enterprise.name)),
            )
            sites = access.visible(
                session,
                user,
                session.scalars(select(Site).order_by(Site.name)),
            )
            companies = [
                {
                    "name": enterprise.name,
                    "sites": [
                        {
                            "id": site.id,
                            "name": site.name,
                            "running": len(
                                services.list_event_frames(
                                    session,
                                    site_id=site.id,
                                    open_only=True,
                                    top_level_only=True,
                                )
                            ),
                        }
                        for site in sites
                        if site.enterprise_id == enterprise.id
                    ],
                }
                for enterprise in enterprises
            ]
            return page(request, "home.html", companies=companies)

    @router.get("/sites/{site_id}")
    def site_page(
        request: Request,
        site_id: int,
        page_number: int = Query(1, alias="page", ge=1),
        running_type: str | None = Query(None, alias="running"),
        finished_type: str | None = Query(None, alias="finished"),
        user: CurrentUser = Depends(signed_in_user),
    ):
        running_type = running_type or None
        finished_type = finished_type or None
        with session_factory() as session:
            site = readable(session, user, Site, site_id)
            running = [
                _batch(session, f)
                for f in access.visible(
                    session,
                    user,
                    services.list_event_frames(
                        session, site_id=site.id, open_only=True
                    ),
                )
            ]
            # Only top-level batches are counted and filtered: a step is
            # shown inside its batch, so filtering on it would hide the
            # batch it belongs to.
            nested = _nested(running)
            running_counts: dict[str, int] = {}
            for batch in nested:
                name = batch["type_name"]
                running_counts[name] = running_counts.get(name, 0) + 1

            # Counted without access.visible: reading a site means reading
            # every batch at it, because access is decided per site.
            per_template = services.count_finished_top_level_by_template(
                session, site.id
            )
            names = {
                template_id: session.get(
                    EventFrameTemplate, template_id
                ).name
                for template_id in per_template
            }
            finished_counts = _count_by_name(per_template, names)
            finished, has_older = _finished(
                session,
                user,
                page_number,
                site_id=site.id,
                top_level_only=True,
                event_frame_template_ids=(
                    None
                    if finished_type is None
                    else [i for i, n in names.items() if n == finished_type]
                ),
            )

            filters = {
                k: v
                for k, v in (
                    ("running", running_type),
                    ("finished", finished_type),
                )
                if v is not None
            }
            here = {
                **filters,
                **({"page": page_number} if page_number > 1 else {}),
            }
            return page(
                request,
                "site.html",
                crumbs=[HOME],
                site=site,
                timezone=resolve_timezone(session, site),
                running=[
                    b
                    for b in nested
                    if running_type is None
                    or b["type_name"] == running_type
                ],
                running_type=running_type,
                running_filter=_type_filter(
                    site.id, here, "running", running_counts, running_type
                ),
                kinds=_equipment_by_kind(session, user, site),
                areas=areas_with_measurements(session, user, site),
                finished=finished,
                finished_type=finished_type,
                finished_filter=_type_filter(
                    site.id, here, "finished", finished_counts, finished_type
                ),
                page_number=page_number,
                has_older=has_older,
                # Paging keeps both filters, so Older doesn't lose your
                # place.
                paging_query=filters,
            )

    @router.get("/equipment/{element_id}")
    def equipment_page(
        request: Request,
        element_id: int,
        page_number: int = Query(1, alias="page", ge=1),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            element = readable(session, user, Element, element_id)
            return _equipment_view(
                request, session, user, element, page_number
            )

    @router.post("/equipment/{element_id}/readings")
    def record_equipment_readings(
        request: Request,
        element_id: int,
        fields: dict = Depends(form_fields),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            element = operable(session, user, Element, element_id)
            tags = [
                session.get(Tag, m["id"])
                for m in element_measurements(session, element)
            ]

            def again(status_code: int = 200, **notice):
                entry = _typed_entry(request, session, element, tags, fields)
                return _equipment_view(
                    request,
                    session,
                    user,
                    element,
                    entry=entry(**notice),
                    status_code=status_code,
                )

            return _record(
                request,
                session,
                tags,
                fields,
                zone_for_element(session, element),
                None,
                None,
                again,
                f"/equipment/{element_id}",
            )

    @router.get("/batches/{batch_id}")
    def batch_page(
        request: Request,
        batch_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = readable(session, user, EventFrame, batch_id)
            return _batch_view(request, session, user, frame)

    @router.post("/batches/{batch_id}/readings")
    def record_batch_readings(
        request: Request,
        batch_id: int,
        fields: dict = Depends(form_fields),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            if frame.ended_at is not None:
                # Its form is gone: most likely closed in another tab.
                return RedirectResponse(
                    f"/batches/{batch_id}", status_code=303
                )
            tags, frames = entry_tags(
                session, measurement_groups(session, user, frame)
            )
            element = session.get(Element, frame.element_id)

            def again(status_code: int = 200, **notice):
                entry = _typed_entry(request, session, element, tags, fields)
                return _batch_view(
                    request,
                    session,
                    user,
                    frame,
                    entry=entry(**notice),
                    status_code=status_code,
                )

            return _record(
                request,
                session,
                tags,
                fields,
                zone_for_frame(session, frame),
                frames,
                frame,
                again,
                f"/batches/{batch_id}",
            )

    @router.post("/batches/{batch_id}/steps")
    def run_batch_step(
        request: Request,
        batch_id: int,
        fields: dict = Depends(form_fields),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = operable(session, user, EventFrame, batch_id)
            if frame.ended_at is not None:
                # Its steps' buttons are gone: closed in another tab.
                return RedirectResponse(
                    f"/batches/{batch_id}", status_code=303
                )
            action = fields.get("action", "")
            label = action_label(session, frame, action)
            zone = zone_for_frame(session, frame)
            tags, frames = entry_tags(
                session, measurement_groups(session, user, frame)
            )
            element = session.get(Element, frame.element_id)

            def again(status_code: int = 200, **notice):
                entry = _typed_entry(request, session, element, tags, fields)
                return _batch_view(
                    request,
                    session,
                    user,
                    frame,
                    entry=entry(action=action, label=label, **notice),
                    status_code=status_code,
                )

            if not spend_token(request, fields.get("token", "")):
                return again(409, error=OUT_OF_DATE)
            # Whatever was typed saves with the press, all or nothing.
            errors, error, warnings = record_entries(
                session, tags, fields, zone, frames, frame, required=False
            )
            if errors or error:
                session.rollback()
                return again(400, errors=errors, error=error)
            recorded = bool(
                [v for v in typed_values(fields, tags).values() if v.strip()]
            )
            when = parse_time(fields.get("observed_at", ""), zone, "")
            try:
                done, more = run_action(session, user, frame, action, when)
            except (NotFoundError, PermissionDeniedError):
                raise
            except ServiceError as exc:
                session.rollback()
                return again(400, error=str(exc))
            for warning in [*future_warning(when, "That time"), *more]:
                if warning not in warnings:
                    warnings.append(warning)
            # The key names the press as well, so accepting a warning for
            # one button doesn't accept it for another.
            if warnings and unaccepted(
                [action, *warnings], fields.get("accepted", "")
            ):
                session.rollback()
                return again(warnings=warnings)
            session.commit()
            notice = f"step_{done}" + ("_recorded" if recorded else "")
            return RedirectResponse(
                f"/batches/{batch_id}?done={notice}#steps",
                status_code=303,
            )

    return router


def _record(
    request, session, tags, fields, zone, frames, own, again, done_url
):
    """Save a page's several-measurements form, or bring the page back.

    ``frames`` names the batch each tag's reading is for, ``own`` the
    page's batch (see ``entry.record_entries``).
    """
    if not spend_token(request, fields.get("token", "")):
        return again(409, error=OUT_OF_DATE)
    errors, error, warnings = record_entries(
        session, tags, fields, zone, frames, own
    )
    if errors or error:
        session.rollback()
        return again(400, errors=errors, error=error)
    if unaccepted(warnings, fields.get("accepted", "")):
        session.rollback()
        return again(warnings=warnings)
    session.commit()
    return RedirectResponse(
        f"{done_url}?done=readings_recorded#measurements", status_code=303
    )


def _typed_entry(request, session, element, tags, fields):
    """The form as it was sent, ready to be shown again with a notice."""
    zone = zone_for_element(session, element)

    def entry(**notice) -> dict:
        return entry_form(
            request,
            zone,
            values=typed_values(fields, tags),
            observed_at=fields.get("observed_at", ""),
            **notice,
        )

    return entry


def _equipment_view(
    request: Request,
    session: Session,
    user: CurrentUser,
    element: Element,
    page_number: int = 1,
    entry: dict | None = None,
    status_code: int = 200,
):
    """A piece of equipment's page, with its measurement form as given."""
    template = session.get(ElementTemplate, element.element_template_id)
    site = session.get(Site, template.site_id)
    zone = resolve_timezone(session, site)
    running = [
        _batch(session, f)
        for f in services.list_event_frames(
            session, element_id=element.id, open_only=True
        )
    ]
    finished, has_older = _finished(
        session, user, page_number, element_id=element.id
    )
    children = access.visible(
        session,
        user,
        services.list_elements(session, parent_id=element.id),
    )
    measurements = element_measurements(session, element)
    if entry is None and measurements:
        entry = entry_form(request, zone)
    return page(
        request,
        "equipment.html",
        status_code=status_code,
        element=element,
        kind=template.name,
        site=site,
        crumbs=trail(session, element, include_self=False),
        inside=_chips(children, _site_busy(session, site.id)),
        measurements=measurements,
        entry=entry,
        timezone=zone,
        running=running,
        finished=finished,
        page_number=page_number,
        has_older=has_older,
        # Only offer what the overlap rule would let through; the
        # service still decides. A brewhouse runs several at once.
        start_types=(
            services.list_event_frame_templates(session, template.id)
            if template.exclusive is False or not running
            else []
        ),
    )


def _batch_view(
    request: Request,
    session: Session,
    user: CurrentUser,
    frame: EventFrame,
    entry: dict | None = None,
    status_code: int = 200,
):
    """A batch's page, with its one form as given.

    While the batch runs, the form holds its measurements' boxes, its
    steps' Start and Finish buttons, and the boxes of each running step
    under it, with one time for all of them.
    """
    element = session.get(Element, frame.element_id)
    template = session.get(ElementTemplate, element.element_template_id)
    site = session.get(Site, template.site_id)
    zone = resolve_timezone(session, site)

    def related(frames: list[EventFrame]) -> list[dict]:
        return [
            _batch(session, f) for f in access.visible(session, user, frames)
        ]

    def one(frame_id: int | None) -> dict | None:
        found = related(
            [session.get(EventFrame, frame_id)] if frame_id else []
        )
        return found[0] if found else None

    groups = measurement_groups(session, user, frame)
    steps = step_rows(session, user, frame)
    attach_groups(session, steps, groups)
    has_boxes = any(
        m["tag_id"] is not None for g in groups for m in g["measurements"]
    )
    # Readings are entered while the batch runs; a finished one's are
    # corrected one at a time, from See all.
    if entry is None and frame.ended_at is None and (has_boxes or steps):
        entry = entry_form(request, zone)
    return page(
        request,
        "batch.html",
        status_code=status_code,
        crumbs=batch_page_trail(session, frame),
        batch=_batch(session, frame),
        properties=batch_properties(session, frame),
        # Reopening is one press, so a finished batch's page carries the
        # pass for it.
        token=mint_token(request) if frame.ended_at is not None else "",
        own=[g for g in groups if g["step_id"] is None],
        entry=entry,
        has_boxes=has_boxes,
        steps=steps,
        site=site,
        timezone=zone,
        parent=one(frame.parent_id),
        source=one(frame.source_event_frame_id),
        destination=one(frame.destination_event_frame_id),
        moved_in=related(
            services.list_event_frames(
                session, destination_event_frame_id=frame.id
            )
        ),
        moved_out=related(
            services.list_event_frames(
                session, source_event_frame_id=frame.id
            )
        ),
    )
