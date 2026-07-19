"""Tests for element attribute wiring: tag paths, find-or-create, resync."""

import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    ElementAttribute,
    Enterprise,
    Lookup,
    MeasurementUnit,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services import (
    ConflictError,
    ValidationError,
    build_tag_name,
    create_element,
    create_element_attribute_template,
    create_element_template,
    create_tag,
    delete_element,
    delete_element_attribute_template,
    get_element,
    list_element_attributes,
    unwire_element_attribute,
    update_element,
    wire_element_attribute,
)

_TS = datetime.datetime(2026, 6, 1, 8, 0, 0)


@pytest.fixture
def ctx():
    """Site with a Cellar > Fermenter template tree, an area, and a unit."""
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
            enterprise_id=ent.id, abbreviation="C", name="Celsius"
        )
        lookup = Lookup(enterprise_id=ent.id, name="Stage")
        session.add_all([area, unit, lookup])
        session.flush()
        cellar_t = create_element_template(session, site.id, "Cellar")
        ferm_t = create_element_template(
            session, site.id, "Fermenter", parent_id=cellar_t.id
        )
        yield session, {
            "site": site.id,
            "area": area.id,
            "unit": unit.id,
            "lookup": lookup.id,
            "cellar_t": cellar_t.id,
            "ferm_t": ferm_t.id,
        }


def _tag_names(session):
    return {t.name for t in session.scalars(select(Tag)).all()}


# -- tag path building -----------------------------------------------------

def test_tag_name_is_full_path(ctx):
    session, ids = ctx
    at = create_element_attribute_template(
        session, ids["ferm_t"], "Temperature",
        measurement_unit_id=ids["unit"],
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    fv01 = create_element(
        session, ids["ferm_t"], "FV01",
        tag_area_id=ids["area"], parent_id=cellar.id,
    )
    assert build_tag_name(session, fv01, at) == "Cellar.FV01.Temperature"


# -- wiring on element create ----------------------------------------------

def test_create_element_wires_attributes(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature",
        measurement_unit_id=ids["unit"],
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    attrs = list_element_attributes(session, element_id=cellar.id)
    assert len(attrs) == 1
    assert attrs[0].owns_tag is True
    tag = session.get(Tag, attrs[0].tag_id)
    assert tag.name == "Cellar.Temperature"
    assert tag.measurement_unit_id == ids["unit"]


def test_create_element_without_tag_area_defers_wiring(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    assert list_element_attributes(session, element_id=cellar.id) == []
    # assigning a tag area wires it
    update_element(session, cellar.id, tag_area_id=ids["area"])
    assert len(list_element_attributes(session, element_id=cellar.id)) == 1


# -- retroactive wiring ----------------------------------------------------

def test_new_attribute_template_wires_existing_elements(ctx):
    session, ids = ctx
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    assert list_element_attributes(session, element_id=cellar.id) == []
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    attrs = list_element_attributes(session, element_id=cellar.id)
    assert len(attrs) == 1
    assert session.get(Tag, attrs[0].tag_id).name == "Cellar.Temperature"


# -- find-or-create: adoption ----------------------------------------------

def test_existing_tag_is_adopted_when_type_matches(ctx):
    session, ids = ctx
    # pre-create the tag the wiring would generate
    existing = create_tag(
        session, ids["area"], "Cellar.Temperature",
        measurement_unit_id=ids["unit"],
    )
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature",
        measurement_unit_id=ids["unit"],
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    attrs = list_element_attributes(session, element_id=cellar.id)
    assert attrs[0].tag_id == existing.id
    assert attrs[0].owns_tag is False  # adopted, not created


def test_existing_tag_with_wrong_type_errors(ctx):
    session, ids = ctx
    create_tag(
        session, ids["area"], "Cellar.Temperature",
        lookup_id=ids["lookup"],  # lookup-typed
    )
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature",
        measurement_unit_id=ids["unit"],  # numeric -- conflicts
    )
    with pytest.raises(ValidationError):
        create_element(
            session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
        )


# -- resync on rename / re-parent ------------------------------------------

def test_rename_element_resyncs_tag_names(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["ferm_t"], "Temperature"
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    fv01 = create_element(
        session, ids["ferm_t"], "FV01",
        tag_area_id=ids["area"], parent_id=cellar.id,
    )
    assert "Cellar.FV01.Temperature" in _tag_names(session)
    update_element(session, fv01.id, name="FV-01")
    assert "Cellar.FV-01.Temperature" in _tag_names(session)


def test_renaming_parent_resyncs_descendant_tags(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["ferm_t"], "Temperature"
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    create_element(
        session, ids["ferm_t"], "FV01",
        tag_area_id=ids["area"], parent_id=cellar.id,
    )
    # renaming the PARENT must rewrite the child's tag name
    update_element(session, cellar.id, name="Main Cellar")
    assert "Main Cellar.FV01.Temperature" in _tag_names(session)


def test_adopted_tags_are_not_renamed(ctx):
    session, ids = ctx
    create_tag(session, ids["area"], "Cellar.Temperature")
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    update_element(session, cellar.id, name="Main Cellar")
    # the adopted tag keeps its original name -- not ours to rename
    assert "Cellar.Temperature" in _tag_names(session)


# -- unwiring / deletes ----------------------------------------------------

def test_unwire_removes_owned_tag(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    attr = list_element_attributes(session, element_id=cellar.id)[0]
    unwire_element_attribute(session, attr.id)
    assert _tag_names(session) == set()


def test_unwire_leaves_owned_tag_that_has_readings(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    attr = list_element_attributes(session, element_id=cellar.id)[0]
    session.add(
        TagValue(tag_id=attr.tag_id, observed_at=_TS, value=64.0)
    )
    session.flush()
    # unwiring succeeds; the history-bearing tag is left standing
    unwire_element_attribute(session, attr.id)
    assert session.get(Tag, attr.tag_id) is not None


def test_unwire_keeps_adopted_tag(ctx):
    session, ids = ctx
    existing = create_tag(session, ids["area"], "Cellar.Temperature")
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    attr = list_element_attributes(session, element_id=cellar.id)[0]
    unwire_element_attribute(session, attr.id)
    assert session.get(Tag, existing.id) is not None


def test_delete_element_leaves_tags_with_readings(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    attr = list_element_attributes(session, element_id=cellar.id)[0]
    session.add(
        TagValue(tag_id=attr.tag_id, observed_at=_TS, value=64.0)
    )
    session.flush()
    # the element goes; its history-bearing tag stays
    delete_element(session, cellar.id)
    assert "Cellar.Temperature" in _tag_names(session)


def test_delete_element_removes_owned_tags_when_clean(ctx):
    session, ids = ctx
    create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    delete_element(session, cellar.id)
    assert _tag_names(session) == set()
    assert session.scalars(select(ElementAttribute)).all() == []


def test_delete_attribute_template_unwires_instances(ctx):
    session, ids = ctx
    at = create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    delete_element_attribute_template(session, at.id)
    assert session.scalars(select(ElementAttribute)).all() == []
    assert _tag_names(session) == set()


# -- manual wiring to an existing tag --------------------------------------

def test_wire_to_explicit_tag_is_not_owned(ctx):
    session, ids = ctx
    at = create_element_attribute_template(
        session, ids["cellar_t"], "Pressure"
    )
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    tag = create_tag(session, ids["area"], "Some.Existing.Tag")
    attr = wire_element_attribute(session, cellar, at, tag_id=tag.id)
    assert attr.owns_tag is False
    assert attr.tag_id == tag.id


def test_cannot_wire_same_template_twice(ctx):
    session, ids = ctx
    at = create_element_attribute_template(
        session, ids["cellar_t"], "Temperature"
    )
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area"]
    )
    # create_element already wired it
    with pytest.raises(ConflictError):
        wire_element_attribute(session, get_element(session, cellar.id), at)
