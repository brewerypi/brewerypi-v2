"""Tests for the tag-value service functions."""

import datetime

import pytest
from sqlalchemy import create_engine
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
from brewerypi.services import (
    NotFoundError,
    ValidationError,
    delete_tag_value,
    get_tag_value,
    update_tag_value,
)

_TS = datetime.datetime(2026, 6, 1, 8, 0, 0)


@pytest.fixture
def ctx():
    """Numeric and lookup-typed tags, each with one reading.

    Yields (session, ids) with numeric_tv, lookup_tv, and the lookup's
    selectable value names available via the models.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        site = Site(abbreviation="S", name="Site", enterprise_id=ent.id)
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        unit = MeasurementUnit(
            enterprise_id=ent.id, abbreviation="°C", name="Celsius"
        )
        lookup = Lookup(enterprise_id=ent.id, name="Stage")
        session.add_all([area, unit, lookup])
        session.flush()
        primary = LookupValue(
            lookup_id=lookup.id, name="Primary", is_selectable=True
        )
        secondary = LookupValue(
            lookup_id=lookup.id, name="Secondary", is_selectable=True
        )
        archived = LookupValue(
            lookup_id=lookup.id, name="Archived", is_selectable=False
        )
        session.add_all([primary, secondary, archived])
        num_tag = Tag(
            name="Mash Temp", area_id=area.id, measurement_unit_id=unit.id
        )
        lk_tag = Tag(name="Stage", area_id=area.id, lookup_id=lookup.id)
        session.add_all([num_tag, lk_tag])
        session.flush()
        num_tv = TagValue(tag_id=num_tag.id, timestamp=_TS, value=64.0)
        lk_tv = TagValue(
            tag_id=lk_tag.id, timestamp=_TS, lookup_value_id=primary.id
        )
        session.add_all([num_tv, lk_tv])
        session.flush()
        yield session, {"num_tv": num_tv.id, "lk_tv": lk_tv.id}


def test_get_and_unknown(ctx):
    session, ids = ctx
    assert get_tag_value(session, ids["num_tv"]).value == 64.0
    with pytest.raises(NotFoundError):
        get_tag_value(session, 9999)


def test_update_numeric_value(ctx):
    session, ids = ctx
    update_tag_value(session, ids["num_tv"], value=66.5)
    assert get_tag_value(session, ids["num_tv"]).value == 66.5


def test_update_numeric_rejects_lookup_value(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        update_tag_value(session, ids["num_tv"], lookup_value="Primary")


def test_update_timestamp(ctx):
    session, ids = ctx
    update_tag_value(
        session, ids["num_tv"], timestamp="2026-06-02T09:30:00"
    )
    got = get_tag_value(session, ids["num_tv"])
    assert got.timestamp == datetime.datetime(2026, 6, 2, 9, 30, 0)


def test_update_bad_timestamp(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        update_tag_value(session, ids["num_tv"], timestamp="not-a-date")


def test_update_lookup_reading(ctx):
    session, ids = ctx
    update_tag_value(session, ids["lk_tv"], lookup_value="Secondary")
    tv = get_tag_value(session, ids["lk_tv"])
    assert tv.lookup_value.name == "Secondary"


def test_update_lookup_rejects_value(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        update_tag_value(session, ids["lk_tv"], value=5.0)


def test_update_lookup_rejects_unselectable(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        update_tag_value(session, ids["lk_tv"], lookup_value="Archived")


def test_delete(ctx):
    session, ids = ctx
    delete_tag_value(session, ids["num_tv"])
    with pytest.raises(NotFoundError):
        get_tag_value(session, ids["num_tv"])
