"""Tests for the tag-value service functions."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Enterprise,
    Lookup,
    LookupValue,
    MeasurementUnit,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services import (
    NotFoundError,
    NotSelectableError,
    ValidationError,
    count_tag_values,
    delete_tag_value,
    get_tag_value,
    list_tag_values,
    record_tag_value,
    selectable_values,
    update_tag_value,
)

_TS = datetime.datetime(2026, 6, 1, 8, 0, 0)


@pytest.fixture
def ctx():
    """Numeric and lookup-typed tags, each with one reading.

    Yields (session, ids) with numeric_tv, lookup_tv, and the lookup's
    selectable value names available via the models.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        site = Site(abbreviation="S", name="Site", enterprise_id=ent.id)
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        unit = MeasurementUnit(
            enterprise_id=ent.id, abbreviation="°C", name="Celsius"
        )
        lookup = Lookup(enterprise_id=ent.id, name="Stage")
        session.add_all([area, unit, lookup])
        session.flush()
        primary = LookupValue(
            lookup_id=lookup.id, name="Primary", is_selectable=True
        )
        secondary = LookupValue(
            lookup_id=lookup.id, name="Secondary", is_selectable=True
        )
        archived = LookupValue(
            lookup_id=lookup.id, name="Archived", is_selectable=False
        )
        session.add_all([primary, secondary, archived])
        num_tag = Tag(
            name="Mash Temp", area_id=area.id, measurement_unit_id=unit.id
        )
        lk_tag = Tag(name="Stage", area_id=area.id, lookup_id=lookup.id)
        session.add_all([num_tag, lk_tag])
        session.flush()
        num_tv = TagValue(tag_id=num_tag.id, observed_at=_TS, value=64.0)
        lk_tv = TagValue(
            tag_id=lk_tag.id, observed_at=_TS, lookup_value_id=primary.id
        )
        session.add_all([num_tv, lk_tv])
        session.flush()
        yield session, {
            "num_tv": num_tv.id, "lk_tv": lk_tv.id,
            "num_tag": num_tag.id, "lk_tag": lk_tag.id,
            "lookup": lookup.id,
        }


def test_record_a_numeric_reading_with_its_author(ctx):
    session, ids = ctx
    later = _TS + datetime.timedelta(hours=1)
    reading = record_tag_value(
        session, ids["num_tag"], later, value=65.5, recorded_by_id=7
    )
    assert reading.id is not None
    assert (reading.value, reading.observed_at) == (65.5, later)
    assert reading.recorded_by_id == 7


def test_record_a_lookup_reading_by_option_name(ctx):
    session, ids = ctx
    reading = record_tag_value(
        session, ids["lk_tag"], _TS, lookup_value="Secondary"
    )
    assert session.get(LookupValue, reading.lookup_value_id).name == (
        "Secondary"
    )


def test_record_refuses_the_wrong_kind_of_value(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        record_tag_value(session, ids["num_tag"], _TS, lookup_value="x")
    with pytest.raises(ValidationError):
        record_tag_value(session, ids["lk_tag"], _TS, value=1.0)
    with pytest.raises(ValidationError):
        record_tag_value(session, ids["num_tag"], _TS)


def test_record_names_the_options_an_unlisted_value_could_be(ctx):
    session, ids = ctx
    with pytest.raises(NotSelectableError) as raised:
        record_tag_value(session, ids["lk_tag"], _TS, lookup_value="Archived")
    assert raised.value.allowed == ["Primary", "Secondary"]


def test_record_on_an_unknown_tag(ctx):
    session, _ = ctx
    with pytest.raises(NotFoundError):
        record_tag_value(session, 9999, _TS, value=1.0)


def test_list_newest_first_within_inclusive_bounds(ctx):
    session, ids = ctx
    hour = datetime.timedelta(hours=1)
    made = [
        record_tag_value(
            session, ids["num_tag"], _TS + n * hour, value=float(n)
        )
        for n in (1, 2, 3)
    ]
    newest_first = list_tag_values(session, ids["num_tag"])
    assert [r.value for r in newest_first] == [3.0, 2.0, 1.0, 64.0]
    assert list_tag_values(
        session, ids["num_tag"], start=_TS + hour, end=_TS + 2 * hour
    ) == [made[1], made[0]]


def test_count_and_oldest_first_share_the_bounds(ctx):
    session, ids = ctx
    hour = datetime.timedelta(hours=1)
    for n in (1, 2, 3):
        record_tag_value(
            session, ids["num_tag"], _TS + n * hour, value=float(n)
        )
    bounds = {"start": _TS + hour, "end": _TS + 3 * hour}
    assert count_tag_values(session, ids["num_tag"]) == 4
    assert count_tag_values(session, ids["num_tag"], **bounds) == 3
    oldest = list_tag_values(
        session, ids["num_tag"], **bounds, oldest_first=True, limit=1
    )
    assert [r.value for r in oldest] == [1.0]


def test_list_pages_without_repeats_when_times_tie(ctx):
    session, ids = ctx
    for n in range(4):
        record_tag_value(session, ids["num_tag"], _TS, value=float(n))
    every = list_tag_values(session, ids["num_tag"])
    pages = [
        list_tag_values(session, ids["num_tag"], limit=2, offset=o)
        for o in (0, 2, 4)
    ]
    assert sum(pages, []) == every


def test_selectable_values_skip_retired_options(ctx):
    session, ids = ctx
    assert selectable_values(session, ids["lookup"]) == [
        "Primary",
        "Secondary",
    ]


def test_get_and_unknown(ctx):
    session, ids = ctx
    assert get_tag_value(session, ids["num_tv"]).value == 64.0
    with pytest.raises(NotFoundError):
        get_tag_value(session, 9999)


def test_update_numeric_value(ctx):
    session, ids = ctx
    update_tag_value(session, ids["num_tv"], value=66.5)
    assert get_tag_value(session, ids["num_tv"]).value == 66.5


def test_update_numeric_rejects_lookup_value(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        update_tag_value(session, ids["num_tv"], lookup_value="Primary")


def test_update_observed_at(ctx):
    session, ids = ctx
    update_tag_value(
        session, ids["num_tv"], observed_at="2026-06-02T09:30:00"
    )
    got = get_tag_value(session, ids["num_tv"])
    assert got.observed_at == datetime.datetime(2026, 6, 2, 9, 30, 0)


def test_update_bad_observed_at(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        update_tag_value(session, ids["num_tv"], observed_at="not-a-date")


def test_update_lookup_reading(ctx):
    session, ids = ctx
    update_tag_value(session, ids["lk_tv"], lookup_value="Secondary")
    tv = get_tag_value(session, ids["lk_tv"])
    assert tv.lookup_value.name == "Secondary"


def test_update_lookup_rejects_value(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        update_tag_value(session, ids["lk_tv"], value=5.0)


def test_update_lookup_rejects_unselectable(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        update_tag_value(session, ids["lk_tv"], lookup_value="Archived")


def test_delete(ctx):
    session, ids = ctx
    delete_tag_value(session, ids["num_tv"])
    with pytest.raises(NotFoundError):
        get_tag_value(session, ids["num_tv"])
