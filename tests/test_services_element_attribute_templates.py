"""Tests for element attribute template services and guard extensions."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Enterprise, Lookup, MeasurementUnit, Site
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_element_attribute_template,
    create_element_template,
    delete_element_attribute_template,
    delete_lookup,
    delete_measurement_unit,
    get_element_attribute_template,
    list_element_attribute_templates,
    update_element_attribute_template,
)


@pytest.fixture
def ctx():
    """Enterprise with a Fermenter template + lookup/unit; a second
    enterprise with its own lookup/unit for cross-enterprise checks."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        e1 = Enterprise(abbreviation="E1", name="Ent One")
        e2 = Enterprise(abbreviation="E2", name="Ent Two")
        session.add_all([e1, e2])
        session.flush()
        site = Site(abbreviation="S", name="Site", enterprise_id=e1.id)
        session.add(site)
        session.flush()
        ferm = create_element_template(session, site.id, "Fermenter")
        lk1 = Lookup(enterprise_id=e1.id, name="Stage")
        lk2 = Lookup(enterprise_id=e2.id, name="Stage2")
        mu1 = MeasurementUnit(
            enterprise_id=e1.id, abbreviation="C", name="Celsius"
        )
        mu2 = MeasurementUnit(
            enterprise_id=e2.id, abbreviation="F", name="Fahrenheit"
        )
        session.add_all([lk1, lk2, mu1, mu2])
        session.flush()
        yield session, {
            "ferm": ferm.id,
            "lk1": lk1.id,
            "mu1": mu1.id,
            "lk2": lk2.id,
            "mu2": mu2.id,
        }


def test_create_numeric(ctx):
    session, ids = ctx
    at = create_element_attribute_template(
        session, ids["ferm"], "Temperature",
        measurement_unit_id=ids["mu1"],
    )
    assert at.id is not None
    assert get_element_attribute_template(session, at.id).name == \
        "Temperature"


def test_create_lookup_typed(ctx):
    session, ids = ctx
    at = create_element_attribute_template(
        session, ids["ferm"], "Stage", lookup_id=ids["lk1"]
    )
    assert at.lookup_id == ids["lk1"]


def test_create_untyped(ctx):
    session, ids = ctx
    at = create_element_attribute_template(session, ids["ferm"], "Note")
    assert at.lookup_id is None
    assert at.measurement_unit_id is None


def test_create_rejects_both_types(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_element_attribute_template(
            session, ids["ferm"], "X",
            lookup_id=ids["lk1"], measurement_unit_id=ids["mu1"],
        )


def test_create_rejects_foreign_lookup(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_element_attribute_template(
            session, ids["ferm"], "X", lookup_id=ids["lk2"]
        )


def test_create_rejects_foreign_unit(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_element_attribute_template(
            session, ids["ferm"], "X", measurement_unit_id=ids["mu2"]
        )


def test_create_unknown_element_template(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        create_element_attribute_template(session, 9999, "X")


def test_create_duplicate_name(ctx):
    session, ids = ctx
    create_element_attribute_template(session, ids["ferm"], "Temperature")
    with pytest.raises(ConflictError):
        create_element_attribute_template(
            session, ids["ferm"], "Temperature"
        )


def test_update_name_and_description(ctx):
    session, ids = ctx
    at = create_element_attribute_template(
        session, ids["ferm"], "Temperature"
    )
    update_element_attribute_template(
        session, at.id, name="Temp", description="in C"
    )
    got = get_element_attribute_template(session, at.id)
    assert got.name == "Temp"
    assert got.description == "in C"


def test_list_filters_by_element_template(ctx):
    session, ids = ctx
    create_element_attribute_template(session, ids["ferm"], "Temperature")
    create_element_attribute_template(session, ids["ferm"], "Pressure")
    names = {
        a.name
        for a in list_element_attribute_templates(
            session, element_template_id=ids["ferm"]
        )
    }
    assert names == {"Temperature", "Pressure"}


def test_delete(ctx):
    session, ids = ctx
    at = create_element_attribute_template(session, ids["ferm"], "Temp")
    delete_element_attribute_template(session, at.id)
    with pytest.raises(NotFoundError):
        get_element_attribute_template(session, at.id)


# -- guard extensions ------------------------------------------------------

def test_delete_lookup_refused_when_attr_template_uses_it(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["ferm"], "Stage", lookup_id=ids["lk1"]
    )
    with pytest.raises(ValidationError):
        delete_lookup(session, ids["lk1"])


def test_delete_unit_refused_when_attr_template_uses_it(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["ferm"], "Temperature",
        measurement_unit_id=ids["mu1"],
    )
    with pytest.raises(ValidationError):
        delete_measurement_unit(session, ids["mu1"])
