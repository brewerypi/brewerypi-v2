"""Viewing batches: what is running, and what has run.

Changing them lives in ``batch_actions``.

Every page checks the row its address names with ``access.check``, and every
list keeps only what the signed-in user may read, so another company's ids
look exactly like ids that don't exist.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi import access, services
from brewerypi.access import READ
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
from brewerypi.services import NotFoundError
from brewerypi.timezones import resolve_timezone
from brewerypi.web.auth import signed_in_user
from brewerypi.web.rendering import elapsed, page

#: How many finished batches a page lists before offering older ones.
PAGE_SIZE = 25


def _readable(session: Session, user: CurrentUser, model: type, row_id: int):
    """Return a row the user may read; anything else is simply not found."""
    row = session.get(model, row_id)
    if row is None:
        raise NotFoundError(f"no {model.__name__} with id {row_id}")
    access.check(session, user, row, READ)
    return row


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


def _type_filters(running: list[dict]) -> list[dict]:
    """The batch types running, by name, with how many of each.

    Only top-level batches count: a step is shown inside its batch, so
    filtering on it would hide the batch it belongs to.
    """
    counts: dict[str, int] = {}
    for batch in running:
        counts[batch["type_name"]] = counts.get(batch["type_name"], 0) + 1
    return [
        {"name": name, "count": counts[name]} for name in sorted(counts)
    ]


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
        batch_type: str | None = Query(None, alias="type"),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            site = _readable(session, user, Site, site_id)
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
            finished, has_older = _finished(
                session,
                user,
                page_number,
                site_id=site.id,
                top_level_only=True,
            )
            nested = _nested(running)
            batch_type = batch_type or None
            return page(
                request,
                "site.html",
                site=site,
                timezone=resolve_timezone(session, site),
                running=[
                    b
                    for b in nested
                    if batch_type is None or b["type_name"] == batch_type
                ],
                running_total=len(nested),
                types=_type_filters(nested),
                batch_type=batch_type,
                kinds=_equipment_by_kind(session, user, site),
                finished=finished,
                page_number=page_number,
                has_older=has_older,
                # Paging keeps the filter, so Older doesn't lose your place.
                paging_query=(
                    {"type": batch_type} if batch_type is not None else {}
                ),
            )

    @router.get("/equipment/{element_id}")
    def equipment_page(
        request: Request,
        element_id: int,
        page_number: int = Query(1, alias="page", ge=1),
        user: CurrentUser = Depends(signed_in_user),
    ):
        with session_factory() as session:
            element = _readable(session, user, Element, element_id)
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
            # Parents share the element's site, so whoever reads it reads
            # them too.
            path = []
            parent_id = element.parent_id
            while parent_id is not None:
                parent = session.get(Element, parent_id)
                path.insert(0, parent)
                parent_id = parent.parent_id
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
                path=path,
                inside=_chips(children, _site_busy(session, site.id)),
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
            frame = _readable(session, user, EventFrame, batch_id)
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
                batch=_batch(session, frame),
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
