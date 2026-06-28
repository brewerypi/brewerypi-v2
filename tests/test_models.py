"""Tests for all domain models — verifies relationships, navigation, and
cascade deletes. Uses an in-memory SQLite database, so no setup or cleanup
is required.
"""

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Enterprise,
    Lookup,
    LookupValue,
    MeasurementUnit,
    Site,
    Tag,
    TagValue,
)


def make_engine():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def test_hierarchy_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        site = Site(abbreviation="HQ", name="Headquarters")
        site.areas.append(Area(abbreviation="MASH", name="Mash House"))
        enterprise.sites.append(site)
        session.add(enterprise)
        session.commit()

        loaded = session.scalar(select(Enterprise))
        assert loaded is not None
        assert [s.name for s in loaded.sites] == ["Headquarters"]
        assert loaded.sites[0].areas[0].name == "Mash House"

        # navigate back up the hierarchy
        area = session.scalar(select(Area))
        assert area.site.enterprise.name == "Brewery Co"

        # deleting the enterprise cascades to its sites and areas
        session.delete(loaded)
        session.commit()
        assert session.scalar(select(func.count()).select_from(Site)) == 0
        assert session.scalar(select(func.count()).select_from(Area)) == 0


def test_lookup_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        lookup = Lookup(name="Fermentation Stage")
        lookup.lookup_values.append(
            LookupValue(name="Primary", is_selectable=True)
        )
        lookup.lookup_values.append(
            LookupValue(name="Secondary", is_selectable=True)
        )
        lookup.lookup_values.append(
            LookupValue(name="Conditioning", is_selectable=False)
        )
        enterprise.lookups.append(lookup)
        session.add(enterprise)
        session.commit()

        # navigate enterprise -> lookup -> lookup_values
        loaded = session.scalar(select(Enterprise))
        assert loaded is not None
        assert [lk.name for lk in loaded.lookups] == ["Fermentation Stage"]
        values = loaded.lookups[0].lookup_values
        assert sorted(v.name for v in values) == [
            "Conditioning",
            "Primary",
            "Secondary",
        ]

        # navigate lookup_value -> lookup -> enterprise
        lv = session.scalar(
            select(LookupValue).where(LookupValue.name == "Primary")
        )
        assert lv.lookup.name == "Fermentation Stage"
        assert lv.lookup.enterprise.name == "Brewery Co"

        # verify is_selectable is stored correctly
        conditioning = session.scalar(
            select(LookupValue).where(LookupValue.name == "Conditioning")
        )
        assert conditioning.is_selectable is False

        # deleting the enterprise cascades to its lookups and lookup_values
        session.delete(loaded)
        session.commit()
        assert (
            session.scalar(select(func.count()).select_from(Lookup)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(LookupValue)) == 0
        )


def test_measurement_unit_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        enterprise.measurement_units.append(
            MeasurementUnit(abbreviation="°C", name="Celsius")
        )
        enterprise.measurement_units.append(
            MeasurementUnit(abbreviation="°P", name="Plato")
        )
        session.add(enterprise)
        session.commit()

        loaded = session.scalar(select(Enterprise))
        names = sorted(mu.name for mu in loaded.measurement_units)
        assert names == ["Celsius", "Plato"]

        mu = session.scalar(
            select(MeasurementUnit).where(MeasurementUnit.name == "Celsius")
        )
        assert mu.enterprise.name == "Brewery Co"
        assert mu.abbreviation == "°C"

        # deleting the enterprise cascades to its measurement_units
        session.delete(loaded)
        session.commit()
        assert (
            session.scalar(
                select(func.count()).select_from(MeasurementUnit)
            ) == 0
        )


def test_numeric_tag_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        mu = MeasurementUnit(abbreviation="°C", name="Celsius")
        enterprise.measurement_units.append(mu)
        site = Site(abbreviation="HQ", name="Headquarters")
        area = Area(abbreviation="BH", name="Brewhouse")
        site.areas.append(area)
        enterprise.sites.append(site)
        session.add(enterprise)
        session.flush()

        tag = Tag(name="Mash Temperature", measurement_unit_id=mu.id)
        area.tags.append(tag)
        session.flush()

        ts = __import__("datetime").datetime(2024, 1, 1, 12, 0, 0)
        tag.tag_values.append(TagValue(timestamp=ts, value=68.5))
        session.commit()

        loaded_tag = session.scalar(select(Tag))
        assert loaded_tag.name == "Mash Temperature"
        assert loaded_tag.lookup is None
        assert loaded_tag.measurement_unit.name == "Celsius"
        assert loaded_tag.area.name == "Brewhouse"

        tv = loaded_tag.tag_values[0]
        assert tv.value == 68.5
        assert tv.lookup_value is None

        # deleting the area cascades to its tags and tag_values
        session.delete(loaded_tag.area)
        session.commit()
        assert session.scalar(select(func.count()).select_from(Tag)) == 0
        assert (
            session.scalar(select(func.count()).select_from(TagValue)) == 0
        )


def test_lookup_tag_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        lookup = Lookup(name="Fermentation Stage")
        lv_primary = LookupValue(name="Primary", is_selectable=True)
        lookup.lookup_values.append(lv_primary)
        enterprise.lookups.append(lookup)
        site = Site(abbreviation="HQ", name="Headquarters")
        area = Area(abbreviation="BH", name="Brewhouse")
        site.areas.append(area)
        enterprise.sites.append(site)
        session.add(enterprise)
        session.flush()

        tag = Tag(name="Fermentation Stage Tag", lookup_id=lookup.id)
        area.tags.append(tag)
        session.flush()

        ts = __import__("datetime").datetime(2024, 1, 1, 12, 0, 0)
        tag.tag_values.append(
            TagValue(timestamp=ts, lookup_value_id=lv_primary.id)
        )
        session.commit()

        loaded_tag = session.scalar(select(Tag))
        assert loaded_tag.lookup.name == "Fermentation Stage"
        assert loaded_tag.measurement_unit is None

        tv = loaded_tag.tag_values[0]
        assert tv.value is None
        assert tv.lookup_value.name == "Primary"

        # deleting the tag cascades to its tag_values
        session.delete(loaded_tag)
        session.commit()
        assert (
            session.scalar(select(func.count()).select_from(TagValue)) == 0
        )
        # lookup_value still exists — tag_values was the only reference
        assert (
            session.scalar(
                select(func.count()).select_from(LookupValue)
            ) == 1
        )


def test_tag_value_requires_exactly_one_value_column():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        site = Site(abbreviation="HQ", name="Headquarters")
        area = Area(abbreviation="BH", name="Brewhouse")
        site.areas.append(area)
        enterprise.sites.append(site)
        session.add(enterprise)
        session.flush()

        tag = Tag(name="Mash Temp")
        area.tags.append(tag)
        session.flush()

        ts = __import__("datetime").datetime(2024, 1, 1, 12, 0, 0)

        # both null — must be rejected
        session.add(TagValue(tag_id=tag.id, timestamp=ts))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        # both non-null — must be rejected
        lookup = Lookup(name="Stage")
        lv = LookupValue(name="Primary", is_selectable=True)
        lookup.lookup_values.append(lv)
        enterprise.lookups.append(lookup)
        session.flush()

        session.add(
            TagValue(
                tag_id=tag.id,
                timestamp=ts,
                value=68.5,
                lookup_value_id=lv.id,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_lookup_value_delete_blocked_when_referenced_by_tag_value():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        lookup = Lookup(name="Fermentation Stage")
        lv = LookupValue(name="Primary", is_selectable=True)
        lookup.lookup_values.append(lv)
        enterprise.lookups.append(lookup)
        site = Site(abbreviation="HQ", name="Headquarters")
        area = Area(abbreviation="BH", name="Brewhouse")
        site.areas.append(area)
        enterprise.sites.append(site)
        session.add(enterprise)
        session.flush()

        tag = Tag(name="Stage Tag", lookup_id=lookup.id)
        area.tags.append(tag)
        session.flush()

        ts = __import__("datetime").datetime(2024, 1, 1, 12, 0, 0)
        tag.tag_values.append(TagValue(timestamp=ts, lookup_value_id=lv.id))
        session.commit()

    # attempt to delete the LookupValue in a fresh session
    with Session(engine) as session:
        lv = session.scalar(select(LookupValue))
        session.delete(lv)
        with pytest.raises(IntegrityError):
            session.commit()
