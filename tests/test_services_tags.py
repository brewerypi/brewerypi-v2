"""Tests for the tag service functions."""

import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Enterprise,
    Lookup,
    MeasurementUnit,
    Site,
    TagValue,
)
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_tag,
    delete_tag,
    get_tag,
    list_tags,
    update_tag,
)


@pytest.fixture
def ctx():
    """Two enterprises, each with a lookup + unit; enterprise 1 has an area.

    Yields (session, ids) where ids has area_id, lk1/mu1 (enterprise 1) and
    lk2/mu2 (enterprise 2, for cross-enterprise checks).
    """
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
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        session.add(area)
        lk1 = Lookup(enterprise_id=e1.id, name="Stage")
        lk2 = Lookup(enterprise_id=e2.id, name="Stage2")
        mu1 = MeasurementUnit(
            enterprise_id=e1.id, abbreviation="°C", name="Celsius"
        )
        mu2 = MeasurementUnit(
            enterprise_id=e2.id, abbreviation="°F", name="Fahrenheit"
        )
        session.add_all([lk1, lk2, mu1, mu2])
        session.flush()
        ids = {
            "area_id": area.id,
            "lk1": lk1.id,
            "mu1": mu1.id,
            "lk2": lk2.id,
            "mu2": mu2.id,
        }
        yield session, ids


def test_create_numeric_tag(ctx):
    session, ids = ctx
    tag = create_tag(
        session, ids["area_id"], "Mash Temp",
        measurement_unit_id=ids["mu1"],
    )
    assert tag.id is not None
    assert get_tag(session, tag.id).measurement_unit_id == ids["mu1"]


def test_create_lookup_tag(ctx):
    session, ids = ctx
    tag = create_tag(
        session, ids["area_id"], "Stage", lookup_id=ids["lk1"]
    )
    assert tag.lookup_id == ids["lk1"]


def test_create_plain_tag(ctx):
    session, ids = ctx
    tag = create_tag(session, ids["area_id"], "Note")
    assert tag.lookup_id is None
    assert tag.measurement_unit_id is None


def test_create_unknown_area(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        create_tag(session, 9999, "Mash Temp")


def test_create_duplicate_name(ctx):
    session, ids = ctx
    create_tag(session, ids["area_id"], "Mash Temp")
    with pytest.raises(ConflictError):
        create_tag(session, ids["area_id"], "Mash Temp")


def test_create_rejects_both_types(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_tag(
            session, ids["area_id"], "X",
            lookup_id=ids["lk1"], measurement_unit_id=ids["mu1"],
        )


def test_create_rejects_foreign_lookup(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_tag(
            session, ids["area_id"], "X", lookup_id=ids["lk2"]
        )


def test_create_rejects_foreign_unit(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_tag(
            session, ids["area_id"], "X", measurement_unit_id=ids["mu2"]
        )


def test_create_unknown_lookup(ctx):
    session, ids = ctx
    with pytest.raises(NotFoundError):
        create_tag(session, ids["area_id"], "X", lookup_id=9999)


def test_update_name_and_description(ctx):
    session, ids = ctx
    tag = create_tag(session, ids["area_id"], "Mash Temp")
    update_tag(session, tag.id, name="Mash Temperature", description="hot")
    got = get_tag(session, tag.id)
    assert got.name == "Mash Temperature"
    assert got.description == "hot"


def test_update_name_conflict(ctx):
    session, ids = ctx
    create_tag(session, ids["area_id"], "A")
    other = create_tag(session, ids["area_id"], "B")
    with pytest.raises(ConflictError):
        update_tag(session, other.id, name="A")


def test_list_tags_filters_by_area(ctx):
    session, ids = ctx
    create_tag(session, ids["area_id"], "A")
    create_tag(session, ids["area_id"], "B")
    names = {t.name for t in list_tags(session, area_id=ids["area_id"])}
    assert names == {"A", "B"}


def test_delete_tag_success(ctx):
    session, ids = ctx
    tag = create_tag(session, ids["area_id"], "Mash Temp")
    delete_tag(session, tag.id)
    with pytest.raises(NotFoundError):
        get_tag(session, tag.id)


def test_delete_tag_cascades_readings(ctx):
    session, ids = ctx
    tag = create_tag(
        session, ids["area_id"], "Mash Temp",
        measurement_unit_id=ids["mu1"],
    )
    session.add(
        TagValue(
            tag_id=tag.id,
            observed_at=datetime.datetime(2026, 6, 1, 8, 0, 0),
            value=64.0,
        )
    )
    session.flush()
    # readings go with the tag (tag_values.tag_id is NOT NULL)
    delete_tag(session, tag.id)
    with pytest.raises(NotFoundError):
        get_tag(session, tag.id)
    remaining = session.scalar(
        select(func.count()).select_from(TagValue)
    )
    assert remaining == 0
