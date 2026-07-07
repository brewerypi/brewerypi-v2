"""Service-layer CRUD for sites.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction; these functions ``flush`` but
never commit.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from brewerypi.models import Area, Enterprise, Site, Tag, TagValue
from brewerypi.services._validation import clean_str, optional_str
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_sites(
    session: Session, enterprise_id: int | None = None
) -> list[Site]:
    """Return sites, optionally filtered by enterprise."""
    stmt = select(Site).order_by(Site.name)
    if enterprise_id is not None:
        stmt = stmt.where(Site.enterprise_id == enterprise_id)
    return list(session.scalars(stmt).all())


def get_site(session: Session, site_id: int) -> Site:
    """Return one site, or raise NotFoundError."""
    site = session.get(Site, site_id)
    if site is None:
        raise NotFoundError(f"no site with id {site_id}")
    return site


def create_site(
    session: Session,
    enterprise_id: int,
    abbreviation: str,
    name: str,
    description: str | None = None,
) -> Site:
    """Create a site under an enterprise (abbreviation/name unique per it)."""
    abbreviation = clean_str(abbreviation, "abbreviation", 10)
    name = clean_str(name, "name", 45)
    if session.get(Enterprise, enterprise_id) is None:
        raise NotFoundError(f"no enterprise with id {enterprise_id}")
    _check_unique(session, enterprise_id, abbreviation, name)
    site = Site(
        enterprise_id=enterprise_id,
        abbreviation=abbreviation,
        name=name,
        description=optional_str(description),
    )
    session.add(site)
    session.flush()
    return site


def update_site(
    session: Session,
    site_id: int,
    abbreviation: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> Site:
    """Update a site; only provided fields change."""
    site = get_site(session, site_id)
    new_abbr = site.abbreviation
    new_name = site.name
    if abbreviation is not None:
        new_abbr = clean_str(abbreviation, "abbreviation", 10)
    if name is not None:
        new_name = clean_str(name, "name", 45)
    _check_unique(
        session, site.enterprise_id, new_abbr, new_name, exclude_id=site_id
    )
    site.abbreviation = new_abbr
    site.name = new_name
    if description is not None:
        site.description = optional_str(description)
    session.flush()
    return site


def delete_site(session: Session, site_id: int) -> None:
    """Delete a site (and its areas/tags), refusing if readings exist below.

    Site -> areas -> tags -> tag_values all cascade, so a delete would
    destroy any recorded history under the site. This refuses when that
    history exists; the areas and tags (config) go with the cascade.
    """
    site = get_site(session, site_id)
    readings = session.scalar(
        select(func.count())
        .select_from(TagValue)
        .join(Tag, TagValue.tag_id == Tag.id)
        .join(Area, Tag.area_id == Area.id)
        .where(Area.site_id == site_id)
    )
    if readings:
        raise ValidationError(
            f"cannot delete site {site_id}: {readings} recorded "
            "reading(s) exist under its areas"
        )
    session.delete(site)
    session.flush()


def _check_unique(
    session: Session,
    enterprise_id: int,
    abbreviation: str,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Raise ConflictError if abbreviation or name is taken in the scope."""
    stmt = select(Site).where(
        Site.enterprise_id == enterprise_id,
        or_(Site.abbreviation == abbreviation, Site.name == name),
    )
    if exclude_id is not None:
        stmt = stmt.where(Site.id != exclude_id)
    existing = session.scalars(stmt).first()
    if existing is not None:
        field = (
            "abbreviation"
            if existing.abbreviation == abbreviation
            else "name"
        )
        raise ConflictError(
            f"a site with that {field} already exists in "
            f"enterprise {enterprise_id}"
        )
