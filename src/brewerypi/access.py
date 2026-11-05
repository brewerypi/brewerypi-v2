"""What the signed-in user may see and change: per-enterprise, per-site.

Every row in the model lives somewhere -- an enterprise, and for most rows
one site within it. ``locate`` finds that place by walking foreign keys up
the hierarchy; ``allowed`` compares it with the caller's grants (a snapshot
of their memberships, taken when the call started, on ``CurrentUser``).

The levels, in increasing order of what they need:

``READ``
    any membership in the enterprise; for a site's rows, that site must be
    in the membership's sites (or it covers all of them).
``OPERATE``
    reading plus recording: readings and batches. Every role may.
``CONFIGURE``
    an admin, at the row's site. A row with no site (the enterprise itself,
    its measurement units and lists) needs an admin of ALL sites, because
    changing it changes every site at once.
``CONFIGURE_ENTERPRISE``
    an admin of all sites, wherever the row is -- creating or deleting a
    site, and managing who belongs to the enterprise.
``SUPERUSER``
    above enterprises: creating or deleting one, and managing users.

Nobody signed in (tests, scripts, ``MCP_AUTH=none``) and superusers are
unrestricted. A row the caller cannot READ is reported as not found, so
another company's ids reveal nothing; a row they can read but not change
is refused with PermissionDeniedError, which says why.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy.orm import Session

from brewerypi.models import (
    Area,
    Element,
    ElementAttribute,
    ElementAttributeTemplate,
    ElementTemplate,
    Enterprise,
    EventFrame,
    EventFrameAttribute,
    EventFrameAttributeTemplate,
    EventFrameTemplate,
    Lookup,
    LookupValue,
    MeasurementUnit,
    Membership,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services.exceptions import NotFoundError, PermissionDeniedError

READ = "read"
OPERATE = "operate"
CONFIGURE = "configure"
CONFIGURE_ENTERPRISE = "configure_enterprise"
SUPERUSER = "superuser"
LEVELS = (READ, OPERATE, CONFIGURE, CONFIGURE_ENTERPRISE, SUPERUSER)

T = TypeVar("T")


@dataclass(frozen=True)
class Grant:
    """One membership, frozen: a role in an enterprise, at some sites."""

    enterprise_id: int
    role: str
    site_ids: frozenset[int] | None  # None = every site, including new ones

    @classmethod
    def from_membership(cls, membership: Membership) -> Grant:
        return cls(
            enterprise_id=membership.enterprise_id,
            role=membership.role,
            site_ids=(
                None
                if membership.has_all_sites
                else frozenset(
                    ms.site_id for ms in membership.membership_sites
                )
            ),
        )

    def covers_site(self, site_id: int | None) -> bool:
        """True for an enterprise-wide row, or a site this grant includes."""
        return (
            site_id is None
            or self.site_ids is None
            or site_id in self.site_ids
        )


@dataclass(frozen=True)
class Place:
    """Where a row lives: its enterprise, and its site if it has one."""

    enterprise_id: int
    site_id: int | None = None


def _place_of_site(session: Session, site_id: int) -> Place | None:
    site = session.get(Site, site_id)
    return Place(site.enterprise_id, site.id) if site is not None else None


def locate(session: Session, row: object) -> Place | None:
    """Return where ``row`` lives, or None if its chain is broken."""
    if isinstance(row, Enterprise):
        return Place(row.id)
    if isinstance(row, Site):
        return Place(row.enterprise_id, row.id)
    if isinstance(row, (MeasurementUnit, Lookup, Membership)):
        return Place(row.enterprise_id)
    if isinstance(row, LookupValue):
        parent = session.get(Lookup, row.lookup_id)
    elif isinstance(row, (Area, ElementTemplate)):
        return _place_of_site(session, row.site_id)
    elif isinstance(row, Tag):
        parent = session.get(Area, row.area_id)
    elif isinstance(row, TagValue):
        parent = session.get(Tag, row.tag_id)
    elif isinstance(
        row, (Element, ElementAttributeTemplate, EventFrameTemplate)
    ):
        parent = session.get(ElementTemplate, row.element_template_id)
    elif isinstance(row, EventFrameAttributeTemplate):
        parent = session.get(EventFrameTemplate, row.event_frame_template_id)
    elif isinstance(row, (ElementAttribute, EventFrame, EventFrameAttribute)):
        parent = session.get(Element, row.element_id)
    else:
        raise TypeError(f"no place is defined for {type(row).__name__}")
    return locate(session, parent) if parent is not None else None


def _unrestricted(user) -> bool:
    return user is None or user.is_superuser


def allowed(user, place: Place, level: str) -> bool:
    """Whether ``user`` (a CurrentUser, or None) may act at ``place``."""
    if level not in LEVELS:
        raise ValueError(f"unknown access level {level!r}")
    if _unrestricted(user):
        return True
    if level == SUPERUSER:
        return False
    grant = next(
        (g for g in user.grants if g.enterprise_id == place.enterprise_id),
        None,
    )
    if grant is None:
        return False
    if level == CONFIGURE_ENTERPRISE or (
        level == CONFIGURE and place.site_id is None
    ):
        return grant.role == "admin" and grant.site_ids is None
    if not grant.covers_site(place.site_id):
        return False
    if level == CONFIGURE:
        return grant.role == "admin"
    return True  # READ and OPERATE: every role


def check(session: Session, user, row: object, level: str) -> None:
    """Refuse ``user`` acting on ``row`` at ``level``.

    Raises NotFoundError when they cannot even see it -- the same answer a
    missing id gets -- and PermissionDeniedError when they can see it but
    not do this to it.
    """
    if _unrestricted(user):
        return
    place = locate(session, row)
    if place is None or not allowed(user, place, READ):
        raise NotFoundError(f"no {_label(row)} with id {row.id}")
    if not allowed(user, place, level):
        raise PermissionDeniedError(_refusal(user, place, level))


def check_rows(
    session: Session,
    user,
    checks: Iterable[tuple[type, int | None, str]],
) -> None:
    """Refuse ``user`` unless every ``(model, id, level)`` named is allowed.

    An id that names no row is let through for the caller to report in its
    own words, so a missing row and an out-of-scope row read the same.
    """
    for model, row_id, level in checks:
        row = session.get(model, row_id) if row_id is not None else None
        if row is not None:
            check(session, user, row, level)


def visible(session: Session, user, rows: Iterable[T]) -> list[T]:
    """Keep the rows ``user`` may read."""
    if _unrestricted(user):
        return list(rows)
    out = []
    for row in rows:
        place = locate(session, row)
        if place is not None and allowed(user, place, READ):
            out.append(row)
    return out


def _label(row: object) -> str:
    """``EventFrameTemplate`` -> "event frame template"."""
    name = type(row).__name__
    return "".join(
        f" {c.lower()}" if c.isupper() and i else c.lower()
        for i, c in enumerate(name)
    )


def _refusal(user, place: Place, level: str) -> str:
    if level == SUPERUSER:
        return "only a Brewery Pi superuser can do that"
    if level == CONFIGURE and place.site_id is not None:
        return (
            f"{user.email} is not an administrator at this site; ask an "
            "administrator to make this change"
        )
    return (
        f"{user.email} is not an administrator of every site in this "
        "company, which this change needs; ask one to make it"
    )
