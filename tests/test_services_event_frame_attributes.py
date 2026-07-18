"""Tests for event frame attribute wiring (element-scoped)."""

import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Enterprise,
    EventFrameAttribute,
    Lookup,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services import (
    ValidationError,
    create_element,
    create_element_attribute_template,
    create_element_template,
    create_event_frame_attribute_template,
    create_event_frame_template,
    create_tag,
    delete_element,
    delete_event_frame_attribute_template,
    list_element_attributes,
    list_event_frame_attributes,
    unwire_element_attribute,
    unwire_event_frame_attribute,
    update_element,
)

_TS = datetime.datetime(2026, 7, 1, 6, 0, 0)


@pytest.fixture
def ctx():
    """Site with a Fermenter template carrying a Fermentation event frame
    template. Yields (session, ids)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        site = Site(
            abbreviation="S", name="Site",
            enterprise_id=ent.id, timezone="UTC",
        )
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        lookup = Lookup(enterprise_id=ent.id, name="FV Status")
        session.add_all([area, lookup])
        session.flush()
        ferm_t = create_element_template(session, site.id, "Fermenter")
        ef_t = create_event_frame_template(
            session, ferm_t.id, "Fermentation"
        )
        yield session, {
            "area": area.id,
            "lookup": lookup.id,
            "ferm_t": ferm_t.id,
            "ef_t": ef_t.id,
        }


def _tag_names(session):
    return {t.name for t in session.scalars(select(Tag)).all()}


# -- wiring on element create ----------------------------------------------

def test_create_element_wires_event_frame_attributes(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["area"]
    )
    wirings = list_event_frame_attributes(session, element_id=fv.id)
    assert len(wirings) == 1
    assert wirings[0].owns_tag is True
    assert session.get(Tag, wirings[0].tag_id).name == "FV01.Status"


def test_no_tag_area_defers_wiring(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    fv = create_element(session, ids["ferm_t"], "FV01")
    assert list_event_frame_attributes(session, element_id=fv.id) == []
    update_element(session, fv.id, tag_area_id=ids["area"])
    assert len(list_event_frame_attributes(session, element_id=fv.id)) == 1


def test_retroactive_wiring_to_existing_elements(ctx):
    session, ids = ctx
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["area"]
    )
    assert list_event_frame_attributes(session, element_id=fv.id) == []
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    assert len(list_event_frame_attributes(session, element_id=fv.id)) == 1


def test_one_wiring_per_element_not_per_frame(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    create_element(session, ids["ferm_t"], "FV01", tag_area_id=ids["area"])
    create_element(session, ids["ferm_t"], "FV02", tag_area_id=ids["area"])
    # two elements -> exactly two wirings, each with its own tag
    assert len(session.scalars(select(EventFrameAttribute)).all()) == 2
    assert {"FV01.Status", "FV02.Status"} <= _tag_names(session)


# -- adoption / sharing with element attributes ----------------------------

def test_adopts_existing_tag(ctx):
    session, ids = ctx
    existing = create_tag(session, ids["area"], "FV01.Status")
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["area"]
    )
    wiring = list_event_frame_attributes(session, element_id=fv.id)[0]
    assert wiring.tag_id == existing.id
    assert wiring.owns_tag is False


def test_type_conflict_rejected(ctx):
    session, ids = ctx
    create_tag(session, ids["area"], "FV01.Status", lookup_id=ids["lookup"])
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    with pytest.raises(ValidationError):
        create_element(
            session, ids["ferm_t"], "FV01", tag_area_id=ids["area"]
        )


def test_shared_tag_not_deleted_by_element_attribute_unwire(ctx):
    """The isReferenced fix: an element attribute and an event frame
    attribute share FV01.Status; unwiring one must not delete the tag."""
    session, ids = ctx
    create_element_attribute_template(session, ids["ferm_t"], "Status")
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["area"]
    )
    el_attr = list_element_attributes(session, element_id=fv.id)[0]
    ef_attr = list_event_frame_attributes(session, element_id=fv.id)[0]
    # both wired to the same tag (one created it, the other adopted)
    assert el_attr.tag_id == ef_attr.tag_id
    unwire_element_attribute(session, el_attr.id)
    # the tag survives because the event frame wiring still references it
    assert session.get(Tag, ef_attr.tag_id) is not None
    assert "FV01.Status" in _tag_names(session)


# -- rename resync ---------------------------------------------------------

def test_rename_element_resyncs_event_frame_tags(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["area"]
    )
    assert "FV01.Status" in _tag_names(session)
    update_element(session, fv.id, name="FV-01")
    assert "FV-01.Status" in _tag_names(session)


# -- unwiring / deletes ----------------------------------------------------

def test_unwire_removes_owned_tag(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["area"]
    )
    wiring = list_event_frame_attributes(session, element_id=fv.id)[0]
    unwire_event_frame_attribute(session, wiring.id)
    assert _tag_names(session) == set()


def test_unwire_refuses_when_owned_tag_has_readings(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["area"]
    )
    wiring = list_event_frame_attributes(session, element_id=fv.id)[0]
    session.add(
        TagValue(tag_id=wiring.tag_id, observed_at=_TS, value=1.0)
    )
    session.flush()
    with pytest.raises(ValidationError):
        unwire_event_frame_attribute(session, wiring.id)


def test_delete_element_unwires_event_frame_attributes(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(session, ids["ef_t"], "Status")
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["area"]
    )
    delete_element(session, fv.id)
    assert session.scalars(select(EventFrameAttribute)).all() == []
    assert _tag_names(session) == set()


def test_delete_attribute_template_unwires(ctx):
    session, ids = ctx
    at = create_event_frame_attribute_template(
        session, ids["ef_t"], "Status"
    )
    create_element(session, ids["ferm_t"], "FV01", tag_area_id=ids["area"])
    delete_event_frame_attribute_template(session, at.id)
    assert session.scalars(select(EventFrameAttribute)).all() == []
    assert _tag_names(session) == set()
