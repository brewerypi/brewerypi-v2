"""Breadcrumbs: where a page sits, from the brewery list down.

A page passes ``crumbs`` (a list of ``href``/``name``) and ``base.html``
draws them, so every page builds its trail the same way. A crumb without
an ``href`` is the page itself, drawn as plain text: the trail always ends
where the user is.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from brewerypi.models import (
    Element,
    ElementTemplate,
    EventFrame,
    EventFrameTemplate,
    Site,
)

HOME = {"href": "/", "name": "Your breweries"}


def here(crumbs: list[dict]) -> list[dict]:
    """The trail of the page its last crumb names: that one is not a link."""
    return [*crumbs[:-1], {"name": crumbs[-1]["name"]}]


def ending_with(crumbs: list[dict], name: str) -> list[dict]:
    """A trail that goes one step further, to a page that is not a link.

    For the pages that do something to the last crumb ("Close", "Edit").
    """
    return [*crumbs, {"name": name}]


def batch_label(session: Session, frame: EventFrame) -> str:
    """A batch as its type and name, "Brew: 1", so it reads on its own."""
    batch_type = session.get(EventFrameTemplate, frame.event_frame_template_id)
    return f"{batch_type.name}: {frame.name}"


def trail(session: Session, element: Element) -> list[dict]:
    """The breadcrumb down to a piece of equipment, through its parents.

    Parents share the element's site, so whoever reads it reads them too.
    """
    template = session.get(ElementTemplate, element.element_template_id)
    site = session.get(Site, template.site_id)
    chain = [element]
    parent_id = element.parent_id
    while parent_id is not None:
        node = session.get(Element, parent_id)
        chain.insert(0, node)
        parent_id = node.parent_id
    return [
        HOME,
        {"href": f"/sites/{site.id}", "name": site.name},
        *({"href": f"/equipment/{e.id}", "name": e.name} for e in chain),
    ]


def batch_page_trail(session: Session, frame: EventFrame) -> list[dict]:
    """The breadcrumb leading to a batch, without the batch itself.

    A top-level batch sits under its equipment. A step sits under the batch
    it is part of, so the way back from a Mashing is its Brew; the step's
    equipment is linked beside its type instead. Parents share the step's
    site, so whoever reads the step reads them too.
    """
    if frame.parent_id is None:
        return trail(session, session.get(Element, frame.element_id))
    parent = session.get(EventFrame, frame.parent_id)
    return [
        *batch_page_trail(session, parent),
        {
            "href": f"/batches/{parent.id}",
            "name": batch_label(session, parent),
        },
    ]


def batch_trail(session: Session, frame: EventFrame) -> list[dict]:
    """The breadcrumb down to a batch, labelled "Brew: 1".

    Its own page passes it through ``here``; the pages that change it keep
    the batch as a link back.
    """
    return [
        *batch_page_trail(session, frame),
        {
            "href": f"/batches/{frame.id}",
            "name": batch_label(session, frame),
        },
    ]
