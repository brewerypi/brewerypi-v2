"""Tests for the enterprise service functions."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Lookup,
    LookupValue,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_enterprise,
    delete_enterprise,
    get_enterprise,
    list_enterprises,
    update_enterprise,
)

_TS = datetime.datetime(2026, 6, 1, 8, 0, 0)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _numeric_reading_under(session, enterprise_id):
    """Build site->area->tag and a numeric reading; return nothing."""
    site = Site(abbreviation="S", name="Site", enterprise_id=enterprise_id)
    session.add(site)
    session.flush()
    area = Area(abbreviation="A", name="Area", site_id=site.id)
    session.add(area)
    session.flush()
    tag = Tag(name="Mash Temp", area_id=area.id)
    session.add(tag)
    session.flush()
    session.add(TagValue(tag_id=tag.id, observed_at=_TS, value=64.0))
    session.flush()


def test_create_and_get(session):
    ent = create_enterprise(session, "BRW", "Brewery Co")
    assert ent.id is not None
    assert get_enterprise(session, ent.id).name == "Brewery Co"


def test_create_duplicate_abbreviation(session):
    create_enterprise(session, "BRW", "Brewery Co")
    with pytest.raises(ConflictError):
        create_enterprise(session, "BRW", "Other Co")


def test_create_duplicate_name(session):
    create_enterprise(session, "BRW", "Brewery Co")
    with pytest.raises(ConflictError):
        create_enterprise(session, "OTH", "Brewery Co")


def test_update(session):
    ent = create_enterprise(session, "BRW", "Brewery Co")
    update_enterprise(session, ent.id, name="Brewery Company")
    assert get_enterprise(session, ent.id).name == "Brewery Company"


def test_update_unknown(session):
    with pytest.raises(NotFoundError):
        update_enterprise(session, 9999, name="X")


def test_list(session):
    create_enterprise(session, "A", "Alpha")
    create_enterprise(session, "B", "Beta")
    assert {e.name for e in list_enterprises(session)} == {"Alpha", "Beta"}


def test_delete_success_with_empty_subtree(session):
    ent = create_enterprise(session, "BRW", "Brewery Co")
    site = Site(abbreviation="S", name="Site", enterprise_id=ent.id)
    session.add(site)
    session.flush()
    delete_enterprise(session, ent.id)  # config only -> cascade ok
    with pytest.raises(NotFoundError):
        get_enterprise(session, ent.id)


def test_delete_refused_when_readings_exist(session):
    ent = create_enterprise(session, "BRW", "Brewery Co")
    _numeric_reading_under(session, ent.id)
    with pytest.raises(ValidationError):
        delete_enterprise(session, ent.id)


def test_delete_refused_when_lookup_value_referenced(session):
    ent = create_enterprise(session, "BRW", "Brewery Co")
    site = Site(abbreviation="S", name="Site", enterprise_id=ent.id)
    session.add(site)
    session.flush()
    area = Area(abbreviation="A", name="Area", site_id=site.id)
    session.add(area)
    lookup = Lookup(enterprise_id=ent.id, name="Stage")
    session.add_all([area, lookup])
    session.flush()
    value = LookupValue(
        lookup_id=lookup.id, name="Primary", is_selectable=True
    )
    session.add(value)
    session.flush()
    tag = Tag(name="Stage", area_id=area.id, lookup_id=lookup.id)
    session.add(tag)
    session.flush()
    session.add(
        TagValue(tag_id=tag.id, observed_at=_TS, lookup_value_id=value.id)
    )
    session.flush()
    with pytest.raises(ValidationError):
        delete_enterprise(session, ent.id)
