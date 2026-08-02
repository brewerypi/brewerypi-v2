"""Tests for the default measurement units seeded onto an enterprise."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Enterprise
from brewerypi.services import (
    DEFAULT_MEASUREMENT_UNITS,
    NotFoundError,
    add_default_measurement_units,
    create_enterprise,
    create_measurement_unit,
    list_measurement_units,
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


def test_definitions_fit_the_columns():
    for abbreviation, name in DEFAULT_MEASUREMENT_UNITS:
        assert len(abbreviation) <= 10, abbreviation
        assert len(name) <= 45, name
        assert abbreviation == abbreviation.strip()
        assert name == name.strip()


def test_definitions_are_unique():
    abbreviations = [a for a, _ in DEFAULT_MEASUREMENT_UNITS]
    names = [n for _, n in DEFAULT_MEASUREMENT_UNITS]
    assert len(set(abbreviations)) == len(abbreviations)
    assert len(set(names)) == len(names)


def test_definitions_are_ordered_by_name():
    names = [n for _, n in DEFAULT_MEASUREMENT_UNITS]
    assert names == sorted(names, key=str.casefold)


def test_add_creates_them_all(ctx):
    session, eid = ctx
    added = add_default_measurement_units(session, eid)
    assert len(added) == len(DEFAULT_MEASUREMENT_UNITS)
    stored = {
        (u.abbreviation, u.name)
        for u in list_measurement_units(session, eid)
    }
    assert stored == set(DEFAULT_MEASUREMENT_UNITS)


def test_add_is_idempotent(ctx):
    session, eid = ctx
    add_default_measurement_units(session, eid)
    assert add_default_measurement_units(session, eid) == []
    assert len(list_measurement_units(session, eid)) == len(
        DEFAULT_MEASUREMENT_UNITS
    )


def test_add_skips_a_conflicting_unit_instead_of_raising(ctx):
    session, eid = ctx
    # Same symbol, the brewery's own name for it.
    create_measurement_unit(session, eid, "°C", "Centigrade")
    added = add_default_measurement_units(session, eid)
    assert len(added) == len(DEFAULT_MEASUREMENT_UNITS) - 1
    assert "Degree Celsius" not in {u.name for u in added}
    units = list_measurement_units(session, eid)
    assert len(units) == len(DEFAULT_MEASUREMENT_UNITS)
    celsius = [u for u in units if u.abbreviation == "°C"]
    assert [u.name for u in celsius] == ["Centigrade"]


def test_add_skips_on_a_name_clash_too(ctx):
    session, eid = ctx
    create_measurement_unit(session, eid, "degC", "Degree Celsius")
    added = add_default_measurement_units(session, eid)
    assert "°C" not in {u.abbreviation for u in added}


def test_add_scopes_to_the_enterprise(ctx):
    session, eid = ctx
    other = Enterprise(abbreviation="OTH", name="Other Co")
    session.add(other)
    session.flush()
    add_default_measurement_units(session, eid)
    assert list_measurement_units(session, other.id) == []


def test_add_requires_an_existing_enterprise(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        add_default_measurement_units(session, 9999)


def test_new_enterprise_gets_the_defaults(ctx):
    session, _ = ctx
    ent = create_enterprise(session, "NEW", "New Co")
    assert len(list_measurement_units(session, ent.id)) == len(
        DEFAULT_MEASUREMENT_UNITS
    )


def test_defaults_can_be_opted_out_of(ctx):
    session, _ = ctx
    ent = create_enterprise(
        session, "NEW", "New Co", include_default_units=False
    )
    assert list_measurement_units(session, ent.id) == []
