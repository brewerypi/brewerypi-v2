"""Tests for the element service functions and the guard extensions."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site, Tag, TagValue
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_element,
    create_element_template,
    delete_area,
    delete_element,
    delete_element_template,
    get_element,
    list_elements,
    update_element,
)


@pytest.fixture
def ctx():
    """Enterprise -> two sites; site1 has a Cellar>Fermenter template tree
    and a tag area. Yields (session, ids)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        s1 = Site(abbreviation="S1", name="Site One", enterprise_id=ent.id)
        s2 = Site(abbreviation="S2", name="Site Two", enterprise_id=ent.id)
        session.add_all([s1, s2])
        session.flush()
        area1 = Area(abbreviation="A1", name="Area", site_id=s1.id)
        area2 = Area(abbreviation="A2", name="Area", site_id=s2.id)
        session.add_all([area1, area2])
        session.flush()
        cellar_t = create_element_template(session, s1.id, "Cellar")
        ferm_t = create_element_template(
            session, s1.id, "Fermenter", parent_id=cellar_t.id
        )
        ids = {
            "s1": s1.id,
            "s2": s2.id,
            "area1": area1.id,
            "area2": area2.id,
            "cellar_t": cellar_t.id,
            "ferm_t": ferm_t.id,
        }
        yield session, ids


# -- A1 mirror on create ---------------------------------------------------

def test_create_top_level_instance(ctx):
    session, ids = ctx
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    assert cellar.id is not None
    assert cellar.parent_id is None


def test_top_level_template_rejects_parent(ctx):
    session, ids = ctx
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    # Fermenter is a child template — but pass a top-level template a parent
    with pytest.raises(ValidationError):
        create_element(
            session, ids["cellar_t"], "Cellar 2", parent_id=cellar.id
        )


def test_child_template_requires_parent(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_element(session, ids["ferm_t"], "FV01")


def test_child_parent_must_instance_parent_template(ctx):
    session, ids = ctx
    # a second Fermenter instance can't parent another Fermenter
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    fv01 = create_element(
        session, ids["ferm_t"], "FV01", parent_id=cellar.id
    )
    with pytest.raises(ValidationError):
        create_element(
            session, ids["ferm_t"], "FV02", parent_id=fv01.id
        )


def test_child_instance_ok(ctx):
    session, ids = ctx
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    fv01 = create_element(
        session, ids["ferm_t"], "FV01", parent_id=cellar.id
    )
    assert fv01.parent_id == cellar.id


def test_create_unknown_template(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        create_element(session, 9999, "X")


# -- tag_area same-site ----------------------------------------------------

def test_tag_area_same_site_ok(ctx):
    session, ids = ctx
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area1"]
    )
    assert cellar.tag_area_id == ids["area1"]


def test_tag_area_other_site_rejected(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_element(
            session, ids["cellar_t"], "Cellar", tag_area_id=ids["area2"]
        )


# -- uniqueness ------------------------------------------------------------

def test_root_unique_within_template(ctx):
    session, ids = ctx
    create_element(session, ids["cellar_t"], "Cellar")
    with pytest.raises(ConflictError):
        create_element(session, ids["cellar_t"], "Cellar")


def test_child_unique_within_parent(ctx):
    session, ids = ctx
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    create_element(session, ids["ferm_t"], "FV01", parent_id=cellar.id)
    with pytest.raises(ConflictError):
        create_element(
            session, ids["ferm_t"], "FV01", parent_id=cellar.id
        )


def test_same_child_name_under_different_parents(ctx):
    session, ids = ctx
    c1 = create_element(session, ids["cellar_t"], "Cellar A")
    c2 = create_element(session, ids["cellar_t"], "Cellar B")
    create_element(session, ids["ferm_t"], "FV01", parent_id=c1.id)
    # same name under a different parent is fine
    create_element(session, ids["ferm_t"], "FV01", parent_id=c2.id)


# -- update ----------------------------------------------------------------

def test_update_name_and_tag_area(ctx):
    session, ids = ctx
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    update_element(
        session, cellar.id, name="Main Cellar", tag_area_id=ids["area1"]
    )
    got = get_element(session, cellar.id)
    assert got.name == "Main Cellar"
    assert got.tag_area_id == ids["area1"]


def test_update_clear_tag_area(ctx):
    session, ids = ctx
    cellar = create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area1"]
    )
    update_element(session, cellar.id, tag_area_id=None)
    assert get_element(session, cellar.id).tag_area_id is None


def test_update_reparent_to_sibling_instance(ctx):
    session, ids = ctx
    c1 = create_element(session, ids["cellar_t"], "Cellar A")
    c2 = create_element(session, ids["cellar_t"], "Cellar B")
    fv = create_element(session, ids["ferm_t"], "FV01", parent_id=c1.id)
    update_element(session, fv.id, parent_id=c2.id)
    assert get_element(session, fv.id).parent_id == c2.id


def test_update_reparent_top_level_element_rejected(ctx):
    session, ids = ctx
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    other = create_element(session, ids["cellar_t"], "Cellar 2")
    # cellar instances a top-level template; giving it a parent breaks A1
    with pytest.raises(ValidationError):
        update_element(session, cellar.id, parent_id=other.id)


# -- delete ----------------------------------------------------------------

def test_delete_leaf(ctx):
    session, ids = ctx
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    delete_element(session, cellar.id)
    with pytest.raises(NotFoundError):
        get_element(session, cellar.id)


def test_delete_refused_with_children(ctx):
    session, ids = ctx
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    create_element(session, ids["ferm_t"], "FV01", parent_id=cellar.id)
    with pytest.raises(ValidationError):
        delete_element(session, cellar.id)


def test_list_filters_by_template(ctx):
    session, ids = ctx
    cellar = create_element(session, ids["cellar_t"], "Cellar")
    create_element(session, ids["ferm_t"], "FV01", parent_id=cellar.id)
    create_element(session, ids["ferm_t"], "FV02", parent_id=cellar.id)
    ferms = list_elements(session, element_template_id=ids["ferm_t"])
    assert {e.name for e in ferms} == {"FV01", "FV02"}


# -- guard extensions ------------------------------------------------------

def test_delete_template_refused_with_instances(ctx):
    session, ids = ctx
    create_element(session, ids["cellar_t"], "Cellar")
    with pytest.raises(ValidationError):
        delete_element_template(session, ids["cellar_t"])


def test_delete_area_refused_when_used_as_tag_area(ctx):
    session, ids = ctx
    create_element(
        session, ids["cellar_t"], "Cellar", tag_area_id=ids["area1"]
    )
    with pytest.raises(ValidationError):
        delete_area(session, ids["area1"])


def test_delete_area_still_refused_on_readings(ctx):
    session, ids = ctx
    tag = Tag(name="Mash Temp", area_id=ids["area1"])
    session.add(tag)
    session.flush()
    session.add(
        TagValue(
            tag_id=tag.id,
            observed_at=datetime.datetime(2026, 6, 1, 8, 0, 0),
            value=64.0,
        )
    )
    session.flush()
    with pytest.raises(ValidationError):
        delete_area(session, ids["area1"])
