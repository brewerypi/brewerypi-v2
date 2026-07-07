"""Tests for the lookup and lookup-value service functions."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Enterprise,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_lookup,
    create_lookup_value,
    delete_lookup,
    delete_lookup_value,
    get_lookup,
    get_lookup_value,
    list_lookup_values,
    list_lookups,
    update_lookup,
    update_lookup_value,
)


@pytest.fixture
def ctx():
    """Session with one enterprise; yields (session, enterprise_id)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="BRW", name="Brewery Co")
        session.add(ent)
        session.flush()
        yield session, ent.id


def _lookup_typed_tag(session, enterprise_id, lookup_id):
    """Create a lookup-typed tag under a fresh site/area; return it."""
    site = Site(abbreviation="HQ", name="HQ", enterprise_id=enterprise_id)
    session.add(site)
    session.flush()
    area = Area(abbreviation="A", name="Area", site_id=site.id)
    session.add(area)
    session.flush()
    tag = Tag(name="Stage", area_id=area.id, lookup_id=lookup_id)
    session.add(tag)
    session.flush()
    return tag


# -- lookups ---------------------------------------------------------------

def test_create_and_get_lookup(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Fermentation Stage")
    assert lk.id is not None
    assert get_lookup(session, lk.id).name == "Fermentation Stage"


def test_create_lookup_unknown_enterprise(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        create_lookup(session, 9999, "Stage")


def test_create_lookup_duplicate_conflicts(ctx):
    session, eid = ctx
    create_lookup(session, eid, "Stage")
    with pytest.raises(ConflictError):
        create_lookup(session, eid, "Stage")


def test_update_lookup(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Stage")
    update_lookup(session, lk.id, name="Fermentation Stage")
    assert get_lookup(session, lk.id).name == "Fermentation Stage"


def test_list_lookups_filters(ctx):
    session, eid = ctx
    create_lookup(session, eid, "Stage")
    create_lookup(session, eid, "Status")
    names = {lk.name for lk in list_lookups(session, enterprise_id=eid)}
    assert names == {"Stage", "Status"}


def test_delete_lookup_success(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Stage")
    create_lookup_value(session, lk.id, "Primary")
    delete_lookup(session, lk.id)
    with pytest.raises(NotFoundError):
        get_lookup(session, lk.id)


def test_delete_lookup_refused_when_tag_uses_it(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Stage")
    _lookup_typed_tag(session, eid, lk.id)
    with pytest.raises(ValidationError):
        delete_lookup(session, lk.id)


def test_delete_lookup_refused_when_value_referenced(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Stage")
    value = create_lookup_value(session, lk.id, "Primary")
    tag = _lookup_typed_tag(session, eid, lk.id)
    session.add(
        TagValue(
            tag_id=tag.id,
            observed_at=datetime.datetime(2026, 6, 1, 8, 0, 0),
            lookup_value_id=value.id,
        )
    )
    session.flush()
    with pytest.raises(ValidationError):
        delete_lookup(session, lk.id)


# -- lookup values ---------------------------------------------------------

def test_create_and_list_values(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Stage")
    create_lookup_value(session, lk.id, "Primary")
    create_lookup_value(session, lk.id, "Secondary", is_selectable=False)
    values = list_lookup_values(session, lk.id)
    assert {v.name for v in values} == {"Primary", "Secondary"}


def test_create_value_unknown_lookup(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        create_lookup_value(session, 9999, "Primary")


def test_create_value_duplicate_conflicts(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Stage")
    create_lookup_value(session, lk.id, "Primary")
    with pytest.raises(ConflictError):
        create_lookup_value(session, lk.id, "Primary")


def test_update_value_name_and_flag(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Stage")
    v = create_lookup_value(session, lk.id, "Primary")
    update_lookup_value(session, v.id, name="Main", is_selectable=False)
    got = get_lookup_value(session, v.id)
    assert got.name == "Main"
    assert got.is_selectable is False


def test_delete_value_success(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Stage")
    v = create_lookup_value(session, lk.id, "Primary")
    delete_lookup_value(session, v.id)
    with pytest.raises(NotFoundError):
        get_lookup_value(session, v.id)


def test_delete_value_refused_when_referenced(ctx):
    session, eid = ctx
    lk = create_lookup(session, eid, "Stage")
    v = create_lookup_value(session, lk.id, "Primary")
    tag = _lookup_typed_tag(session, eid, lk.id)
    session.add(
        TagValue(
            tag_id=tag.id,
            observed_at=datetime.datetime(2026, 6, 1, 8, 0, 0),
            lookup_value_id=v.id,
        )
    )
    session.flush()
    with pytest.raises(ValidationError):
        delete_lookup_value(session, v.id)
