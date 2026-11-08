"""Breadcrumbs: where a page sits, from the brewery list down.

A page passes ``crumbs`` (a list of ``href``/``name``) and ``base.html``
draws them, so every page builds its trail the same way.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from brewerypi.models import Element, ElementTemplate, EventFrame, Site

HOME = {"href": "/", "name": "Your breweries"}


def trail(
    session: Session, element: Element, include_self: bool = True
) -> list[dict]:
    """The breadcrumb down to a piece of equipment, through its parents.

    Parents share the element's site, so whoever reads it reads them too.
    """
    template = session.get(ElementTemplate, element.element_template_id)
    site = session.get(Site, template.site_id)
    chain: list[Element] = []
    node = element if include_self else None
    parent_id = element.parent_id
    if node is not None:
        chain.append(node)
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
    """The breadcrumb above a batch's own page.

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
        {"href": f"/batches/{parent.id}", "name": parent.name},
    ]


def batch_trail(session: Session, frame: EventFrame) -> list[dict]:
    """The breadcrumb down to a batch, for the pages that change it."""
    return [
        *batch_page_trail(session, frame),
        {"href": f"/batches/{frame.id}", "name": frame.name},
    ]
