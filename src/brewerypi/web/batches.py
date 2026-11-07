"""Viewing batches: what is running, and what has run.

Changing them lives in ``batch_actions``.

Every page checks the row its address names with ``access.check``, and every
list keeps only what the signed-in user may read, so another company's ids
look exactly like ids that don't exist.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
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
)
from brewerypi.serializers import event_frame_dict
from brewerypi.timezones import resolve_timezone
from brewerypi.web.auth import signed_in_user
from brewerypi.web.forms import mint_token, readable
from brewerypi.web.measurements import (
    areas_with_measurements,
    batch_measurements,
    element_measurements,
)
from brewerypi.web.navigation import HOME, trail
from brewerypi.web.rendering import elapsed, page

#: How many finished batches a page lists before offering older ones.
PAGE_SIZE = 25


def _batch(session: Session, frame: EventFrame) -> dict:
    """A batch as the pages show it: the serialized row plus its context."""
    batch_type = session.get(EventFrameTemplate, frame.event_frame_template_id)
    until = frame.ended_at or datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        **event_frame_dict(session, frame),
        "type_name": batch_type.name if batch_type is not None else "Batch",
        "elapsed": elapsed(frame.started_at, until),
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
            template = session.get(
                ElementTemplate, element.element_template_id
            )
            site = session.get(Site, template.site_id)
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
            return page(
                request,
                "equipment.html",
                element=element,
                kind=template.name,
                site=site,
                crumbs=trail(session, element, include_self=False),
                inside=_chips(children, _site_busy(session, site.id)),
                measurements=element_measurements(session, element),
                timezone=resolve_timezone(session, site),
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

    @router.get("/batches/{batch_id}")
    def batch_page(
        request: Request,
        batch_id: int,
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            frame = readable(session, user, EventFrame, batch_id)
            element = session.get(Element, frame.element_id)
            template = session.get(
                ElementTemplate, element.element_template_id
            )
            site = session.get(Site, template.site_id)

            def related(frames: list[EventFrame]) -> list[dict]:
                return [
                    _batch(session, f)
                    for f in access.visible(session, user, frames)
                ]

            def one(frame_id: int | None) -> dict | None:
                found = related(
                    [session.get(EventFrame, frame_id)] if frame_id else []
                )
                return found[0] if found else None

            return page(
                request,
                "batch.html",
                crumbs=trail(session, element),
                batch=_batch(session, frame),
                # Reopening is one press, so a finished batch's page carries
                # the pass for it.
                token=(
                    mint_token(request) if frame.ended_at is not None else ""
                ),
                measurements=batch_measurements(session, frame),
                site=site,
                timezone=resolve_timezone(session, site),
                parent=one(frame.parent_id),
                source=one(frame.source_event_frame_id),
                destination=one(frame.destination_event_frame_id),
                steps=sorted(
                    related(
                        services.list_event_frames(
                            session, parent_id=frame.id
                        )
                    ),
                    key=lambda b: b["started_at"],
                ),
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

    return router
