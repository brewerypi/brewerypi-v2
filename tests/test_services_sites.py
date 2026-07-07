"""Tests for the site service functions."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Tag, TagValue
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_site,
    delete_site,
    get_site,
    list_sites,
    update_site,
)


@pytest.fixture
def ctx():
    """Session with one enterprise; yields (session, enterprise_id)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E1", name="Ent One")
        session.add(ent)
        session.flush()
        yield session, ent.id


def test_create_and_get(ctx):
    session, eid = ctx
    site = create_site(session, eid, "HQ", "Headquarters")
    assert site.id is not None
    assert get_site(session, site.id).name == "Headquarters"


def test_create_unknown_enterprise(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        create_site(session, 9999, "HQ", "Headquarters")


def test_create_duplicate_abbreviation(ctx):
    session, eid = ctx
    create_site(session, eid, "HQ", "Headquarters")
    with pytest.raises(ConflictError):
        create_site(session, eid, "HQ", "Home Quarter")


def test_create_duplicate_name(ctx):
    session, eid = ctx
    create_site(session, eid, "HQ", "Headquarters")
    with pytest.raises(ConflictError):
        create_site(session, eid, "HD", "Headquarters")


def test_update(ctx):
    session, eid = ctx
    site = create_site(session, eid, "HQ", "Headquarters")
    update_site(session, site.id, name="Main Plant")
    assert get_site(session, site.id).name == "Main Plant"


def test_update_conflict(ctx):
    session, eid = ctx
    create_site(session, eid, "HQ", "Headquarters")
    other = create_site(session, eid, "W2", "West Plant")
    with pytest.raises(ConflictError):
        update_site(session, other.id, name="Headquarters")


def test_list_filters_by_enterprise(ctx):
    session, eid = ctx
    create_site(session, eid, "HQ", "Headquarters")
    create_site(session, eid, "W2", "West Plant")
    names = {s.name for s in list_sites(session, enterprise_id=eid)}
    assert names == {"Headquarters", "West Plant"}


def test_delete_success_with_empty_subtree(ctx):
    session, eid = ctx
    site = create_site(session, eid, "HQ", "Headquarters")
    area = Area(abbreviation="A", name="Area", site_id=site.id)
    session.add(area)
    session.flush()
    session.add(Tag(name="Mash Temp", area_id=area.id))
    session.flush()
    delete_site(session, site.id)  # no readings -> cascade ok
    with pytest.raises(NotFoundError):
        get_site(session, site.id)


def test_delete_refused_when_readings_exist(ctx):
    session, eid = ctx
    site = create_site(session, eid, "HQ", "Headquarters")
    area = Area(abbreviation="A", name="Area", site_id=site.id)
    session.add(area)
    session.flush()
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
        delete_site(session, site.id)
