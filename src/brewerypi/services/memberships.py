"""Service-layer CRUD for memberships and their site restrictions.

A membership gives one user one role in one enterprise. It covers every site
of that enterprise (``has_all_sites``) unless restricted to a list of them;
the list lives in ``membership_sites``, a pure join table managed only
through these functions, so it has no module of its own.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi.models import (
    MEMBERSHIP_ROLES,
    Enterprise,
    Membership,
    MembershipSite,
    Site,
    User,
)
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_memberships(
    session: Session,
    user_id: int | None = None,
    enterprise_id: int | None = None,
) -> list[Membership]:
    """Return memberships, optionally filtered by user and/or enterprise."""
    stmt = select(Membership).order_by(
        Membership.enterprise_id, Membership.user_id
    )
    if user_id is not None:
        stmt = stmt.where(Membership.user_id == user_id)
    if enterprise_id is not None:
        stmt = stmt.where(Membership.enterprise_id == enterprise_id)
    return list(session.scalars(stmt).all())


def get_membership(session: Session, membership_id: int) -> Membership:
    """Return one membership, or raise NotFoundError."""
    membership = session.get(Membership, membership_id)
    if membership is None:
        raise NotFoundError(f"no membership with id {membership_id}")
    return membership


def find_membership(
    session: Session, user_id: int, enterprise_id: int
) -> Membership | None:
    """Return the user's membership in an enterprise, if they have one."""
    return session.scalar(
        select(Membership).where(
            Membership.user_id == user_id,
            Membership.enterprise_id == enterprise_id,
        )
    )


def create_membership(
    session: Session,
    user_id: int,
    enterprise_id: int,
    role: str,
    site_ids: Iterable[int] | None = None,
) -> Membership:
    """Give a user a role in an enterprise (one membership per pair).

    ``site_ids`` None means every site, including future ones; a list
    restricts the membership to those sites of the enterprise.
    """
    role = _clean_role(role)
    if session.get(User, user_id) is None:
        raise NotFoundError(f"no user with id {user_id}")
    if session.get(Enterprise, enterprise_id) is None:
        raise NotFoundError(f"no enterprise with id {enterprise_id}")
    if find_membership(session, user_id, enterprise_id) is not None:
        raise ConflictError(
            f"user {user_id} already has a membership in enterprise "
            f"{enterprise_id}"
        )
    wanted = _check_sites(session, enterprise_id, site_ids)
    membership = Membership(
        user_id=user_id, enterprise_id=enterprise_id, role=role
    )
    _apply_sites(membership, wanted)
    session.add(membership)
    session.flush()
    return membership


def update_membership(
    session: Session,
    membership_id: int,
    role: str | None = None,
    has_all_sites: bool | None = None,
    site_ids: Iterable[int] | None = None,
) -> Membership:
    """Update a membership; only provided fields change.

    ``site_ids`` replaces the restriction (and implies
    ``has_all_sites=False``); ``has_all_sites=True`` lifts it. An empty
    ``site_ids`` is allowed and means no sites -- the user keeps the
    enterprise-wide reads only.
    """
    membership = get_membership(session, membership_id)
    if has_all_sites and site_ids is not None:
        raise ValidationError(
            "give either has_all_sites=true or a site list, not both"
        )
    new_role = _clean_role(role) if role is not None else None
    wanted = _check_sites(session, membership.enterprise_id, site_ids)
    if new_role is not None:
        membership.role = new_role
    if has_all_sites:
        _apply_sites(membership, None)
    elif wanted is not None:
        _apply_sites(membership, wanted)
    elif has_all_sites is False:
        # Restrict to whatever is granted now (nothing, if it was open).
        membership.has_all_sites = False
    session.flush()
    return membership


def delete_membership(session: Session, membership_id: int) -> None:
    """Remove a user's membership (and its site list)."""
    session.delete(get_membership(session, membership_id))
    session.flush()


def membership_site_ids(membership: Membership) -> list[int] | None:
    """The site ids a membership is restricted to, or None for all sites."""
    if membership.has_all_sites:
        return None
    return sorted(ms.site_id for ms in membership.membership_sites)


def _check_sites(
    session: Session, enterprise_id: int, site_ids: Iterable[int] | None
) -> set[int] | None:
    """Validate a site list before anything changes; None stays None."""
    if site_ids is None:
        return None
    wanted = set(site_ids)
    for site_id in sorted(wanted):
        site = session.get(Site, site_id)
        if site is None:
            raise NotFoundError(f"no site with id {site_id}")
        if site.enterprise_id != enterprise_id:
            raise ValidationError(
                f"site {site_id} belongs to a different enterprise"
            )
    return wanted


def _apply_sites(membership: Membership, wanted: set[int] | None) -> None:
    """Set the membership to all sites (None) or exactly ``wanted``."""
    if wanted is None:
        membership.has_all_sites = True
        membership.membership_sites.clear()
        return
    membership.has_all_sites = False
    for ms in list(membership.membership_sites):
        if ms.site_id not in wanted:
            membership.membership_sites.remove(ms)
    have = {ms.site_id for ms in membership.membership_sites}
    for site_id in sorted(wanted - have):
        membership.membership_sites.append(MembershipSite(site_id=site_id))


def _clean_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role not in MEMBERSHIP_ROLES:
        raise ValidationError(
            f"role must be one of {', '.join(MEMBERSHIP_ROLES)}"
        )
    return role
