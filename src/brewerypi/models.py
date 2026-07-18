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

ElementTemplate is a site-scoped, self-referential template tree
(a top-level template has no parent). Element instances a template
(FV01, FV02 of a Fermenter template); its own parent tree mirrors the
template tree, and tag_area_id points at where its tags are stored.
ElementAttributeTemplate defines an attribute on an element template
(name + optional lookup or measurement unit, like Tag). ElementAttribute
is not yet added; it will gain a tag_id FK when added.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    String,
    UniqueConstraint,
    text,
)
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
    # IANA zone (e.g. "America/New_York"); readings are stored UTC and
    # converted at the tool boundary. Backfilled to "UTC" for existing rows.
    timezone: Mapped[str] = mapped_column(
        String(64), default="UTC", server_default="UTC"
    )

    enterprise: Mapped[Enterprise] = relationship(back_populates="sites")
    areas: Mapped[list[Area]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
    )
    element_templates: Mapped[list[ElementTemplate]] = relationship(
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
    # 255 (not 45) to hold generated element attribute tag paths, e.g.
    # "Cellar.FV01.Temperature".
    name: Mapped[str] = mapped_column(String(255))
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
    # No cascade: the FK is RESTRICT, so a wired tag can't be deleted while
    # an element attribute points at it (the service layer unwires first).
    element_attributes: Mapped[list[ElementAttribute]] = relationship(
        back_populates="tag",
        passive_deletes="all",
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


class ElementTemplate(Base):
    __tablename__ = "element_templates"
    __table_args__ = (UniqueConstraint("site_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id"), index=True
    )
    # Self-referential parent: NULL for a top-level template.
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("element_templates.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(45))
    description: Mapped[str | None] = mapped_column(String(255))
    # Single-occupancy: when True, event frames on an element of this template
    # may not overlap in time (across any template). False = unlimited
    # concurrency (an umbrella like a brewhouse). Backfilled True.
    exclusive: Mapped[bool] = mapped_column(
        default=True, server_default=text("1")
    )

    site: Mapped[Site] = relationship(back_populates="element_templates")
    parent: Mapped[ElementTemplate | None] = relationship(
        back_populates="children",
        remote_side="ElementTemplate.id",
    )
    children: Mapped[list[ElementTemplate]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    elements: Mapped[list[Element]] = relationship(
        back_populates="element_template",
        cascade="all, delete-orphan",
    )
    attribute_templates: Mapped[list[ElementAttributeTemplate]] = (
        relationship(
            back_populates="element_template",
            cascade="all, delete-orphan",
        )
    )
    event_frame_templates: Mapped[list[EventFrameTemplate]] = relationship(
        back_populates="element_template",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<ElementTemplate {self.name!r}>"


class Element(Base):
    __tablename__ = "elements"
    # Child names are unique within their parent element. Root elements
    # (parent_id NULL) are kept unique within their element_template by the
    # service layer, since a plain unique constraint can't span NULL parents.
    __table_args__ = (UniqueConstraint("parent_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    element_template_id: Mapped[int] = mapped_column(
        ForeignKey("element_templates.id"), index=True
    )
    # Where this element's tags will be stored; assignable later.
    tag_area_id: Mapped[int | None] = mapped_column(
        ForeignKey("areas.id"), index=True
    )
    # Self-referential parent: NULL for a top-level element.
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("elements.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(45))
    description: Mapped[str | None] = mapped_column(String(255))

    element_template: Mapped[ElementTemplate] = relationship(
        back_populates="elements"
    )
    tag_area: Mapped[Area | None] = relationship()
    parent: Mapped[Element | None] = relationship(
        back_populates="children",
        remote_side="Element.id",
    )
    children: Mapped[list[Element]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    attributes: Mapped[list[ElementAttribute]] = relationship(
        back_populates="element",
        cascade="all, delete-orphan",
    )
    event_frames: Mapped[list[EventFrame]] = relationship(
        back_populates="element",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Element {self.name!r}>"


class ElementAttributeTemplate(Base):
    __tablename__ = "element_attribute_templates"
    __table_args__ = (UniqueConstraint("element_template_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    element_template_id: Mapped[int] = mapped_column(
        ForeignKey("element_templates.id"), index=True
    )
    # Type: lookup-typed (lookup_id) or numeric (measurement_unit_id) or
    # neither -- mutually exclusive, mirroring Tag. Both must belong to the
    # template's enterprise (enforced in the service layer).
    lookup_id: Mapped[int | None] = mapped_column(
        ForeignKey("lookups.id"), index=True
    )
    measurement_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("measurement_units.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(45))
    description: Mapped[str | None] = mapped_column(String(255))

    element_template: Mapped[ElementTemplate] = relationship(
        back_populates="attribute_templates"
    )
    element_attributes: Mapped[list[ElementAttribute]] = relationship(
        back_populates="element_attribute_template",
        cascade="all, delete-orphan",
    )
    lookup: Mapped[Lookup | None] = relationship()
    measurement_unit: Mapped[MeasurementUnit | None] = relationship()

    def __repr__(self) -> str:
        return f"<ElementAttributeTemplate {self.name!r}>"


class ElementAttribute(Base):
    """An attribute template realized on one element, wired to a tag.

    ``owns_tag`` records how the tag got here: True when the app auto-created
    it for this attribute (so it is removed with the attribute, provided it
    has no readings), False when an existing tag was adopted by name (the tag
    predates this attribute and may be shared, so only the link is removed).
    """

    __tablename__ = "element_attributes"
    __table_args__ = (
        UniqueConstraint("element_id", "element_attribute_template_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    element_id: Mapped[int] = mapped_column(
        ForeignKey("elements.id"), index=True
    )
    element_attribute_template_id: Mapped[int] = mapped_column(
        ForeignKey("element_attribute_templates.id"), index=True
    )
    # RESTRICT: a tag can't be deleted out from under a wired attribute.
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="RESTRICT"), index=True
    )
    owns_tag: Mapped[bool] = mapped_column(default=True)

    element: Mapped[Element] = relationship(back_populates="attributes")
    element_attribute_template: Mapped[ElementAttributeTemplate] = (
        relationship(back_populates="element_attributes")
    )
    tag: Mapped[Tag] = relationship(
        back_populates="element_attributes",
        passive_deletes="all",
    )

    def __repr__(self) -> str:
        return (
            f"<ElementAttribute element_id={self.element_id!r}"
            f" tag_id={self.tag_id!r}>"
        )


class EventFrameTemplate(Base):
    """A type of event frame (batch window) defined for an element template.

    Self-referential like ElementTemplate: a "Brew" template on a Brewhouse
    can have a "Mashing" child template on the Brewhouse's Mash Mixer child
    (the A1 mirror rule is enforced in the service layer). Name is unique per
    element template.
    """

    __tablename__ = "event_frame_templates"
    __table_args__ = (UniqueConstraint("element_template_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    element_template_id: Mapped[int] = mapped_column(
        ForeignKey("element_templates.id"), index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("event_frame_templates.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(45))
    description: Mapped[str | None] = mapped_column(String(255))

    element_template: Mapped[ElementTemplate] = relationship(
        back_populates="event_frame_templates"
    )
    parent: Mapped[EventFrameTemplate | None] = relationship(
        back_populates="children",
        remote_side="EventFrameTemplate.id",
    )
    children: Mapped[list[EventFrameTemplate]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    attribute_templates: Mapped[list[EventFrameAttributeTemplate]] = (
        relationship(
            back_populates="event_frame_template",
            cascade="all, delete-orphan",
        )
    )

    def __repr__(self) -> str:
        return f"<EventFrameTemplate {self.name!r}>"


class EventFrameAttributeTemplate(Base):
    """An attribute defined on an event frame template.

    Same type pattern as an element attribute template (lookup-typed / numeric
    / neither, mutually exclusive, same-enterprise). Additionally carries a
    default start value and a default end value, which are written as readings
    on the wired tag at the frame's ``started_at`` and ``ended_at``. The
    defaults mirror TagValue's storage: a float (numeric) or a lookup value
    (lookup-typed); the lookup-value defaults must belong to ``lookup_id``.
    """

    __tablename__ = "event_frame_attribute_templates"
    __table_args__ = (
        UniqueConstraint("event_frame_template_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_frame_template_id: Mapped[int] = mapped_column(
        ForeignKey("event_frame_templates.id"), index=True
    )
    lookup_id: Mapped[int | None] = mapped_column(
        ForeignKey("lookups.id"), index=True
    )
    measurement_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("measurement_units.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(45))
    description: Mapped[str | None] = mapped_column(String(255))
    # Defaults written as readings at started_at / ended_at. Numeric attrs use
    # the float columns; lookup-typed attrs use the lookup-value FKs.
    default_start_value: Mapped[float | None] = mapped_column()
    default_end_value: Mapped[float | None] = mapped_column()
    default_start_lookup_value_id: Mapped[int | None] = mapped_column(
        ForeignKey("lookup_values.id"), index=True
    )
    default_end_lookup_value_id: Mapped[int | None] = mapped_column(
        ForeignKey("lookup_values.id"), index=True
    )

    event_frame_template: Mapped[EventFrameTemplate] = relationship(
        back_populates="attribute_templates"
    )
    lookup: Mapped[Lookup | None] = relationship()
    measurement_unit: Mapped[MeasurementUnit | None] = relationship()

    def __repr__(self) -> str:
        return f"<EventFrameAttributeTemplate {self.name!r}>"


class EventFrame(Base):
    """An instance of an event frame template: one batch window on an element.

    Half-open interval [started_at, ended_at); a NULL ended_at means the frame
    is open (running). Times are stored UTC (naive) and converted at the tool
    boundary. Nests like the element/template trees (the A1 mirror and the
    overlap/containment guards live in the service layer). The name is a free
    label (not uniqueness-constrained).
    """

    __tablename__ = "event_frames"
    __table_args__ = (
        CheckConstraint(
            "ended_at IS NULL OR ended_at > started_at",
            name="ended_after_started",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    element_id: Mapped[int] = mapped_column(
        ForeignKey("elements.id"), index=True
    )
    event_frame_template_id: Mapped[int] = mapped_column(
        ForeignKey("event_frame_templates.id"), index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("event_frames.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(45))
    started_at: Mapped[datetime] = mapped_column()
    ended_at: Mapped[datetime | None] = mapped_column()

    element: Mapped[Element] = relationship(back_populates="event_frames")
    event_frame_template: Mapped[EventFrameTemplate] = relationship()
    parent: Mapped[EventFrame | None] = relationship(
        back_populates="children",
        remote_side="EventFrame.id",
    )
    children: Mapped[list[EventFrame]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<EventFrame {self.name!r}>"
