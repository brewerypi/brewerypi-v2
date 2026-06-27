"""BreweryPi domain models — Enterprise, Site, Area.

A SQLAlchemy 2.0 translation of the top of the BreweryPi equipment hierarchy
(github.com/brewerypi/brewerypi), restyled to this repo's naming conventions:

  - plural, snake_case tables       (enterprises, sites, areas)
  - `id` primary keys               (not EnterpriseId / SiteId / AreaId)
  - `<parent>_id` foreign keys      (enterprise_id, site_id)
  - lowercase snake_case columns    (abbreviation, description, name)
  - constraints named automatically by the convention on Base.metadata

Hierarchy (a containment chain):  Enterprise 1——* Site 1——* Area.

In BreweryPi a name and an abbreviation are unique *within their parent*, not
globally — so those uniqueness rules are composite constraints including the
parent's foreign key. Relationships to tables not yet defined (Lookups,
ElementTemplates, Elements, Tags) are intentionally omitted; add them later.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brewerypi.database import Base


class Enterprise(Base):
    __tablename__ = "enterprises"

    id: Mapped[int] = mapped_column(primary_key=True)
    abbreviation: Mapped[str] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(45), unique=True)
    description: Mapped[str | None] = mapped_column(String(255))

    # Enterprise 1 ——* Site. Deleting an enterprise removes its sites (and, in
    # turn, their areas), mirroring BreweryPi's cascading delete.
    sites: Mapped[list[Site]] = relationship(
        back_populates="enterprise",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Enterprise {self.name!r}>"


class Site(Base):
    __tablename__ = "sites"
    # Column order is chosen so the two constraints get distinct names
    # under the convention (uq_sites_abbreviation, uq_sites_enterprise_id).
    # Both enforce "unique within the enterprise."
    __table_args__ = (
        UniqueConstraint("abbreviation", "enterprise_id"),
        UniqueConstraint("enterprise_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id"), index=True
    )
    abbreviation: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(45))
    description: Mapped[str | None] = mapped_column(String(255))

    enterprise: Mapped[Enterprise] = relationship(back_populates="sites")

    # Site 1 ——* Area.
    areas: Mapped[list[Area]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Site {self.name!r}>"


class Area(Base):
    __tablename__ = "areas"
    __table_args__ = (
        UniqueConstraint("abbreviation", "site_id"),
        UniqueConstraint("name", "site_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    abbreviation: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(45))
    description: Mapped[str | None] = mapped_column(String(255))

    site: Mapped[Site] = relationship(back_populates="areas")

    def __repr__(self) -> str:
        return f"<Area {self.name!r}>"
