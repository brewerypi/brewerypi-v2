"""Tests for the dicts consumers show: local times, names, and authors."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.identity import CurrentUser, signed_in_as
from brewerypi.models import Area, Enterprise, Tag, TagValue
from brewerypi.serializers import (
    event_frame_dict,
    person_name,
    reading_dict,
    user_dict,
)
from brewerypi.services import (
    create_element,
    create_element_template,
    create_event_frame,
    create_event_frame_template,
    create_membership,
    create_site,
    create_user,
)

# 18:00 UTC is noon in Chicago in January.
STARTED_AT = datetime.datetime(2026, 1, 15, 18, 0)
STARTED_LOCAL = "2026-01-15T12:00:00-06:00"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _enterprise(session, abbreviation="BRW"):
    ent = Enterprise(abbreviation=abbreviation, name=f"Co {abbreviation}")
    session.add(ent)
    session.flush()
    return ent


def _site(session):
    ent = _enterprise(session)
    return create_site(
        session, ent.id, "ATL", "Atlanta", timezone="America/Chicago"
    )


def _frame(session, started_by_id=None):
    site = _site(session)
    template = create_element_template(session, site.id, "Fermenter")
    element = create_element(session, template.id, "FV01")
    batch_type = create_event_frame_template(
        session, template.id, "Fermentation"
    )
    return create_event_frame(
        session,
        element.id,
        batch_type.id,
        "Batch 1240",
        started_at=STARTED_AT,
        started_by_id=started_by_id,
    )


def _tag(session):
    area = Area(site_id=_site(session).id, abbreviation="C", name="Cellar")
    session.add(area)
    session.flush()
    tag = Tag(area_id=area.id, name="FV01.Gravity")
    session.add(tag)
    session.flush()
    return tag


def test_person_name_prefers_the_name_then_the_email(session):
    named = create_user(session, "sam@brewery.com", name="Sam")
    unnamed = create_user(session, "pat@brewery.com")
    session.flush()
    assert person_name(session, named.id) == "Sam"
    assert person_name(session, unnamed.id) == "pat@brewery.com"
    assert person_name(session, None) is None


def test_event_frame_dict_shows_local_time_vessel_and_author(session):
    user = create_user(session, "sam@brewery.com", name="Sam")
    session.flush()
    result = event_frame_dict(session, _frame(session, user.id))
    assert result["name"] == "Batch 1240"
    assert result["element_name"] == "FV01"
    assert result["started_at"] == STARTED_LOCAL
    assert result["timezone"] == "America/Chicago"
    assert result["ended_at"] is None
    assert result["open"] is True
    assert result["started_by"] == "Sam"
    assert result["ended_by"] is None


def test_event_frame_dict_follows_the_signed_in_users_zone(session):
    frame = _frame(session)
    me = CurrentUser(
        id=1, email="a@b.com", timezone="Europe/London", is_superuser=False
    )
    with signed_in_as(me):
        result = event_frame_dict(session, frame)
    assert result["started_at"] == "2026-01-15T18:00:00+00:00"
    assert result["timezone"] == "Europe/London"


def test_reading_dict_shows_local_time_and_author(session):
    user = create_user(session, "sam@brewery.com", name="Sam")
    tag = _tag(session)
    reading = TagValue(
        tag_id=tag.id,
        observed_at=STARTED_AT,
        value=12.4,
        recorded_by_id=user.id,
    )
    session.add(reading)
    session.flush()
    result = reading_dict(session, reading)
    assert result["observed_at"] == STARTED_LOCAL
    assert result["timezone"] == "America/Chicago"
    assert result["value"] == 12.4
    assert result["type"] == "numeric"
    assert result["recorded_by"] == "Sam"
    assert result["updated_by"] is None


def test_user_dict_lists_only_the_memberships_in_scope(session):
    mine = _enterprise(session, "A")
    theirs = _enterprise(session, "B")
    user = create_user(session, "sam@brewery.com")
    create_membership(session, user.id, mine.id, "operator")
    create_membership(session, user.id, theirs.id, "operator")
    session.flush()
    scoped = user_dict(user, {mine.id})
    assert [m["enterprise_id"] for m in scoped["memberships"]] == [mine.id]
    assert len(user_dict(user, None)["memberships"]) == 2
