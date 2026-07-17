"""Tests for timezone conversion helpers and the site timezone field."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Enterprise
from brewerypi.services import (
    ValidationError,
    create_site,
    get_site,
    update_site,
)
from brewerypi.timezones import (
    from_utc,
    is_valid_timezone,
    resolve_timezone,
    to_utc,
)

# -- helpers ---------------------------------------------------------------

def test_is_valid_timezone():
    assert is_valid_timezone("America/New_York")
    assert is_valid_timezone("UTC")
    assert not is_valid_timezone("Mars/Olympus_Mons")
    assert not is_valid_timezone("EST5EDT_bogus")


def test_to_utc_from_naive_local():
    # 8am in New York (EST, winter) is 13:00 UTC
    assert to_utc("2026-01-15T08:00:00", "America/New_York") == \
        datetime.datetime(2026, 1, 15, 13, 0, 0)


def test_to_utc_respects_dst():
    # 8am in New York in July (EDT) is 12:00 UTC
    assert to_utc("2026-07-15T08:00:00", "America/New_York") == \
        datetime.datetime(2026, 7, 15, 12, 0, 0)


def test_to_utc_trusts_explicit_offset():
    # an offset-aware input is converted as given, ignoring tz_name
    assert to_utc("2026-01-15T08:00:00+00:00", "America/New_York") == \
        datetime.datetime(2026, 1, 15, 8, 0, 0)


def test_to_utc_rejects_garbage():
    with pytest.raises(ValidationError):
        to_utc("not-a-time", "UTC")


def test_from_utc_formats_local():
    stored = datetime.datetime(2026, 1, 15, 13, 0, 0)  # UTC
    assert from_utc(stored, "America/New_York") == \
        "2026-01-15T08:00:00-05:00"


def test_round_trip():
    tz = "America/Chicago"
    local = "2026-03-10T09:30:00"
    assert from_utc(to_utc(local, tz), tz).startswith("2026-03-10T09:30:00")


# -- site timezone field + resolver ----------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ent = Enterprise(abbreviation="E", name="Ent")
        s.add(ent)
        s.flush()
        yield s, ent.id


def test_create_site_defaults_utc(session):
    s, eid = session
    site = create_site(s, eid, "HQ", "Headquarters")
    assert site.timezone == "UTC"


def test_create_site_with_timezone(session):
    s, eid = session
    site = create_site(
        s, eid, "HQ", "Headquarters", timezone="America/New_York"
    )
    assert site.timezone == "America/New_York"


def test_create_site_rejects_bad_timezone(session):
    s, eid = session
    with pytest.raises(ValidationError):
        create_site(s, eid, "HQ", "HQ", timezone="Nowhere/Nothing")


def test_update_site_timezone(session):
    s, eid = session
    site = create_site(s, eid, "HQ", "HQ")
    update_site(s, site.id, timezone="Europe/London")
    assert get_site(s, site.id).timezone == "Europe/London"


def test_resolve_timezone_returns_site_zone(session):
    s, eid = session
    site = create_site(
        s, eid, "HQ", "HQ", timezone="America/Los_Angeles"
    )
    assert resolve_timezone(s, site) == "America/Los_Angeles"
