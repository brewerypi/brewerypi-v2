"""Tests for the measurement-unit service functions."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site, Tag
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_measurement_unit,
    delete_measurement_unit,
    get_measurement_unit,
    list_measurement_units,
    update_measurement_unit,
)


@pytest.fixture
def ctx():
    """A session with one enterprise; yields (session, enterprise_id)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="BRW", name="Brewery Co")
        session.add(ent)
        session.flush()
        yield session, ent.id


def test_create_and_get(ctx):
    session, eid = ctx
    unit = create_measurement_unit(session, eid, "°C", "Celsius")
    assert unit.id is not None
    got = get_measurement_unit(session, unit.id)
    assert got.name == "Celsius"
    assert got.abbreviation == "°C"


def test_create_requires_existing_enterprise(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        create_measurement_unit(session, 9999, "°C", "Celsius")


def test_create_rejects_blank_name(ctx):
    session, eid = ctx
    with pytest.raises(ValidationError):
        create_measurement_unit(session, eid, "°C", "   ")


def test_create_duplicate_name_conflicts(ctx):
    session, eid = ctx
    create_measurement_unit(session, eid, "°C", "Celsius")
    with pytest.raises(ConflictError):
        create_measurement_unit(session, eid, "C2", "Celsius")


def test_create_duplicate_abbreviation_conflicts(ctx):
    session, eid = ctx
    create_measurement_unit(session, eid, "°C", "Celsius")
    with pytest.raises(ConflictError):
        create_measurement_unit(session, eid, "°C", "Centigrade")


def test_list_filters_by_enterprise(ctx):
    session, eid = ctx
    create_measurement_unit(session, eid, "°C", "Celsius")
    create_measurement_unit(session, eid, "°F", "Fahrenheit")
    units = list_measurement_units(session, enterprise_id=eid)
    assert {u.name for u in units} == {"Celsius", "Fahrenheit"}


def test_update_changes_fields(ctx):
    session, eid = ctx
    unit = create_measurement_unit(session, eid, "°C", "Celsius")
    update_measurement_unit(session, unit.id, name="Degrees Celsius")
    assert get_measurement_unit(session, unit.id).name == "Degrees Celsius"


def test_update_conflict(ctx):
    session, eid = ctx
    create_measurement_unit(session, eid, "°C", "Celsius")
    other = create_measurement_unit(session, eid, "°F", "Fahrenheit")
    with pytest.raises(ConflictError):
        update_measurement_unit(session, other.id, name="Celsius")


def test_update_unknown_raises(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        update_measurement_unit(session, 9999, name="X")


def test_delete_success(ctx):
    session, eid = ctx
    unit = create_measurement_unit(session, eid, "°C", "Celsius")
    delete_measurement_unit(session, unit.id)
    with pytest.raises(NotFoundError):
        get_measurement_unit(session, unit.id)


def test_delete_refused_when_referenced(ctx):
    session, eid = ctx
    unit = create_measurement_unit(session, eid, "°C", "Celsius")
    site = Site(abbreviation="HQ", name="HQ", enterprise_id=eid)
    session.add(site)
    session.flush()
    area = Area(abbreviation="A", name="Area", site_id=site.id)
    session.add(area)
    session.flush()
    tag = Tag(name="T", area_id=area.id, measurement_unit_id=unit.id)
    session.add(tag)
    session.flush()
    with pytest.raises(ValidationError):
        delete_measurement_unit(session, unit.id)
