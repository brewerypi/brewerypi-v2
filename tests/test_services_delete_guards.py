"""Tests for the delete-guard fixes (clean refusals, no raw FK errors)."""

import datetime

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    ElementAttribute,
    Enterprise,
    EventFrameAttribute,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services import (
    ValidationError,
    create_element,
    create_element_attribute_template,
    create_element_template,
    create_event_frame,
    create_event_frame_attribute_template,
    create_event_frame_template,
    create_tag,
    delete_area,
    delete_event_frame_template,
    delete_tag,
    list_element_attributes,
    unwire_element_attribute,
    wire_element_attribute,
)

_TS = datetime.datetime(2026, 7, 1, 6, 0, 0)


@pytest.fixture
def ctx():
    """Foreign keys ON, so a missing guard shows up as a raw FK error."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

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
        a1 = Area(abbreviation="A1", name="Area One", site_id=site.id)
        a2 = Area(abbreviation="A2", name="Area Two", site_id=site.id)
        session.add_all([a1, a2])
        session.flush()
        ferm_t = create_element_template(session, site.id, "Fermenter")
        yield session, {
            "site": site.id, "a1": a1.id, "a2": a2.id,
            "ferm_t": ferm_t.id, "ent": ent.id,
        }


# -- delete_tag ------------------------------------------------------------

def test_delete_tag_cascades_its_readings(ctx):
    session, ids = ctx
    tag = create_tag(session, ids["a1"], "Loose.Tag")
    session.add(TagValue(tag_id=tag.id, observed_at=_TS, value=1.0))
    session.flush()
    delete_tag(session, tag.id)
    assert session.scalar(
        select(func.count()).select_from(TagValue)
    ) == 0


def test_delete_tag_refused_while_wired(ctx):
    session, ids = ctx
    create_element_attribute_template(session, ids["ferm_t"], "Temperature")
    create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["a1"]
    )
    tag = session.scalars(select(Tag)).one()
    with pytest.raises(ValidationError):
        delete_tag(session, tag.id)


# -- delete_area -----------------------------------------------------------

def test_delete_area_refused_when_its_tag_is_wired_cross_area(ctx):
    """Manual wiring can point an attribute at a tag in another area."""
    session, ids = ctx
    at = create_element_attribute_template(
        session, ids["ferm_t"], "Pressure"
    )
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["a1"]
    )
    auto = list_element_attributes(session, element_id=fv.id)[0]
    unwire_element_attribute(session, auto.id)
    foreign_tag = create_tag(session, ids["a2"], "Other.Tag")
    wire_element_attribute(session, fv, at, tag_id=foreign_tag.id)
    # no element uses Area Two as its tag area, but a tag in it is wired
    with pytest.raises(ValidationError):
        delete_area(session, ids["a2"])


def test_delete_area_still_allowed_when_nothing_wired(ctx):
    session, ids = ctx
    create_tag(session, ids["a2"], "Plain.Tag")
    delete_area(session, ids["a2"])
    assert session.scalar(select(func.count()).select_from(Tag)) == 0


# -- unwiring no longer refuses -------------------------------------------

def test_unwire_keeps_tag_with_readings(ctx):
    session, ids = ctx
    create_element_attribute_template(session, ids["ferm_t"], "Temperature")
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["a1"]
    )
    attr = list_element_attributes(session, element_id=fv.id)[0]
    session.add(TagValue(tag_id=attr.tag_id, observed_at=_TS, value=1.0))
    session.flush()
    unwire_element_attribute(session, attr.id)
    assert session.scalars(select(ElementAttribute)).all() == []
    assert session.get(Tag, attr.tag_id) is not None


def test_unwire_removes_disposable_tag(ctx):
    session, ids = ctx
    create_element_attribute_template(session, ids["ferm_t"], "Temperature")
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["a1"]
    )
    attr = list_element_attributes(session, element_id=fv.id)[0]
    unwire_element_attribute(session, attr.id)
    assert session.scalar(select(func.count()).select_from(Tag)) == 0


# -- delete_event_frame_template ------------------------------------------

def test_event_frame_template_refused_with_instances(ctx):
    session, ids = ctx
    eft = create_event_frame_template(
        session, ids["ferm_t"], "Fermentation"
    )
    fv = create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["a1"]
    )
    create_event_frame(
        session, fv.id, eft.id, "Batch 1", started_at=_TS
    )
    # previously a raw IntegrityError
    with pytest.raises(ValidationError):
        delete_event_frame_template(session, eft.id)


def test_event_frame_template_delete_does_not_orphan_tags(ctx):
    session, ids = ctx
    eft = create_event_frame_template(
        session, ids["ferm_t"], "Fermentation"
    )
    create_event_frame_attribute_template(session, eft.id, "Gravity")
    create_element(
        session, ids["ferm_t"], "FV01", tag_area_id=ids["a1"]
    )
    assert session.scalar(select(func.count()).select_from(Tag)) == 1
    delete_event_frame_template(session, eft.id)
    # the wiring is gone AND its owned tag was cleaned up, not orphaned
    assert session.scalars(select(EventFrameAttribute)).all() == []
    assert session.scalar(select(func.count()).select_from(Tag)) == 0
