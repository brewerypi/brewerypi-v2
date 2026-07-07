"""Tests for the area service functions."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Enterprise, Site, Tag, TagValue
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_area,
    delete_area,
    get_area,
    list_areas,
    update_area,
)


@pytest.fixture
def ctx():
    """Session with an enterprise + site; yields (session, site_id)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E1", name="Ent One")
        session.add(ent)
        session.flush()
        site = Site(abbreviation="S", name="Site", enterprise_id=ent.id)
        session.add(site)
        session.flush()
        yield session, site.id


def test_create_and_get(ctx):
    session, sid = ctx
    area = create_area(session, sid, "BH", "Brewhouse")
    assert area.id is not None
    assert get_area(session, area.id).name == "Brewhouse"


def test_create_unknown_site(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        create_area(session, 9999, "BH", "Brewhouse")


def test_create_duplicate_abbreviation(ctx):
    session, sid = ctx
    create_area(session, sid, "BH", "Brewhouse")
    with pytest.raises(ConflictError):
        create_area(session, sid, "BH", "Bright House")


def test_create_duplicate_name(ctx):
    session, sid = ctx
    create_area(session, sid, "BH", "Brewhouse")
    with pytest.raises(ConflictError):
        create_area(session, sid, "BR", "Brewhouse")


def test_update(ctx):
    session, sid = ctx
    area = create_area(session, sid, "BH", "Brewhouse")
    update_area(session, area.id, name="Brew House")
    assert get_area(session, area.id).name == "Brew House"


def test_update_conflict(ctx):
    session, sid = ctx
    create_area(session, sid, "BH", "Brewhouse")
    other = create_area(session, sid, "CR", "Cellar")
    with pytest.raises(ConflictError):
        update_area(session, other.id, name="Brewhouse")


def test_list_filters_by_site(ctx):
    session, sid = ctx
    create_area(session, sid, "BH", "Brewhouse")
    create_area(session, sid, "CR", "Cellar")
    names = {a.name for a in list_areas(session, site_id=sid)}
    assert names == {"Brewhouse", "Cellar"}


def test_delete_success_with_empty_tags(ctx):
    session, sid = ctx
    area = create_area(session, sid, "BH", "Brewhouse")
    session.add(Tag(name="Mash Temp", area_id=area.id))
    session.flush()
    delete_area(session, area.id)  # tag has no readings -> cascade ok
    with pytest.raises(NotFoundError):
        get_area(session, area.id)


def test_delete_refused_when_readings_exist(ctx):
    session, sid = ctx
    area = create_area(session, sid, "BH", "Brewhouse")
    tag = Tag(name="Mash Temp", area_id=area.id)
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
        delete_area(session, area.id)
