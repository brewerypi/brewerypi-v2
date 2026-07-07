"""BreweryPi domain models.

Hierarchy (containment chain):  Enterprise 1——* Site 1——* Area.
Lookup hierarchy:               Enterprise 1——* Lookup 1——* LookupValue.
Measurement units:              Enterprise 1——* MeasurementUnit.
Tag hierarchy:                  Area 1——* Tag 1——* TagValue.
  A Tag optionally references a Lookup (lookup-typed tag) or a
  MeasurementUnit (numeric tag).  For lookup-typed tags, TagValue stores the
  selected LookupValue via lookup_value_id (FK with RESTRICT — deleting a
  LookupValue that has recorded history is blocked).  For numeric tags,
  TagValue stores the measured float in the value column.

Relationships to ElementTemplate / Element / ElementAttribute are
intentionally omitted; ElementAttribute will gain a tag_id FK when those
tables are added.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from brewerypi.database import Base


class Enterprise(Base):
    __tablename__ = "enterprises"

    id: Mapped[int] = mapped_column(primary_key=True)
    abbreviation: Mapped[str] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(45), unique=True)
    description: Mapped[str | None] = mapped_column(String(255))

    sites: Mapped[list[Site]] = relationship(
        back_populates="enterprise",
        cascade="all, delete-orphan",
    )
    lookups: Mapped[list[Lookup]] = relationship(
        back_populates="enterprise",
        cascade="all, delete-orphan",
    )
    measurement_units: Mapped[list[MeasurementUnit]] = relationship(
        back_populates="enterprise",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Enterprise {self.name!r}>"


class Site(Base):
    __tablename__ = "sites"
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
    tags: Mapped[list[Tag]] = relationship(
        back_populates="area",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Area {self.name!r}>"


class Lookup(Base):
    __tablename__ = "lookups"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(
        ForeignKey("enterprises.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(45))

    enterprise: Mapped[Enterprise] = relationship(back_populates="lookups")
    lookup_values: Mapped[list[LookupValue]] = relationship(
        back_populates="lookup",
        cascade="all, delete-orphan",
    )
    tags: Mapped[list[Tag]] = relationship(back_populates="lookup")

    def __repr__(self) -> str:
        return f"<Lookup {self.name!r}>"


class LookupValue(Base):
    __tablename__ = "lookup_values"
    __table_args__ = (UniqueConstraint("name", "lookup_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lookup_id: Mapped[int] = mapped_column(
        ForeignKey("lookups.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(45))
    is_selectable: Mapped[bool] = mapped_column()

    lookup: Mapped[Lookup] = relationship(back_populates="lookup_values")
    # passive_deletes=True lets the DB enforce ON DELETE RESTRICT rather than
    # having SQLAlchemy null out lookup_value_id before the DELETE.
    tag_values: Mapped[list[TagValue]] = relationship(
        back_populates="lookup_value",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<LookupValue {self.name!r}>"


class MeasurementUnit(Base):
    __tablename__ = "measurement_units"
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

    enterprise: Mapped[Enterprise] = relationship(
        back_populates="measurement_units"
    )
    tags: Mapped[list[Tag]] = relationship(back_populates="measurement_unit")

    def __repr__(self) -> str:
        return f"<MeasurementUnit {self.name!r}>"


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("area_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    area_id: Mapped[int] = mapped_column(ForeignKey("areas.id"), index=True)
    lookup_id: Mapped[int | None] = mapped_column(
        ForeignKey("lookups.id"), index=True
    )
    measurement_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("measurement_units.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(45))
    description: Mapped[str | None] = mapped_column(String(255))

    area: Mapped[Area] = relationship(back_populates="tags")
    lookup: Mapped[Lookup | None] = relationship(back_populates="tags")
    measurement_unit: Mapped[MeasurementUnit | None] = relationship(
        back_populates="tags"
    )
    tag_values: Mapped[list[TagValue]] = relationship(
        back_populates="tag",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Tag {self.name!r}>"


class TagValue(Base):
    __tablename__ = "tag_values"
    __table_args__ = (
        CheckConstraint(
            "(value IS NOT NULL) + (lookup_value_id IS NOT NULL) = 1",
            name="value_xor_lookup_value_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column()
    value: Mapped[float | None] = mapped_column()
    lookup_value_id: Mapped[int | None] = mapped_column(
        ForeignKey("lookup_values.id", ondelete="RESTRICT"), index=True
    )

    tag: Mapped[Tag] = relationship(back_populates="tag_values")
    lookup_value: Mapped[LookupValue | None] = relationship(
        back_populates="tag_values"
    )

    def __repr__(self) -> str:
        return (
            f"<TagValue tag_id={self.tag_id!r}"
            f" observed_at={self.observed_at!r}>"
        )
