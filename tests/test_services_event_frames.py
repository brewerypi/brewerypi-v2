"""Tests for the event frame lifecycle service."""

import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Enterprise,
    LookupValue,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    close_event_frame,
    create_element,
    create_element_template,
    create_event_frame,
    create_event_frame_attribute_template,
    create_event_frame_template,
    create_lookup,
    create_lookup_value,
    delete_event_frame,
    get_event_frame,
    list_event_frames,
    reopen_event_frame,
    update_event_frame,
)


def D(day, hour, minute=0):
    return datetime.datetime(2026, 7, day, hour, minute)


@pytest.fixture
def ctx():
    """Brewhouse (non-exclusive) with a Mash Mixer child (exclusive);
    a Brew template with a Mashing child. Yields (session, ids)."""
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
        session.add(area)
        session.flush()
        bh_t = create_element_template(
            session, site.id, "Brewhouse", exclusive=False
        )
        mm_t = create_element_template(
            session, site.id, "Mash Mixer", parent_id=bh_t.id
        )
        brew_t = create_event_frame_template(session, bh_t.id, "Brew")
        mash_t = create_event_frame_template(
            session, mm_t.id, "Mashing", parent_id=brew_t.id
        )
        bh = create_element(session, bh_t.id, "BH1", tag_area_id=area.id)
        mm = create_element(
            session, mm_t.id, "MM1",
            tag_area_id=area.id, parent_id=bh.id,
        )
        yield session, {
            "area": area.id, "ent": ent.id,
            "bh_t": bh_t.id, "mm_t": mm_t.id,
            "brew_t": brew_t.id, "mash_t": mash_t.id,
            "bh": bh.id, "mm": mm.id,
        }


# -- the brewhouse scenario ------------------------------------------------

def test_concurrent_brews_on_non_exclusive_brewhouse(ctx):
    session, ids = ctx
    create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    # a second brew starts while the first is still running -- allowed
    b2 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #2", started_at=D(17, 8)
    )
    assert b2.id is not None
    assert len(list_event_frames(session, element_id=ids["bh"])) == 2


def test_mashing_queues_on_exclusive_mixer(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    create_event_frame(
        session, ids["mm"], ids["mash_t"], "Mashing",
        started_at=D(17, 6), ended_at=D(17, 7, 30), parent_id=b1.id,
    )
    b2 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #2", started_at=D(17, 8)
    )
    # 8:00 mashing is fine -- the mixer freed up at 7:30
    m2 = create_event_frame(
        session, ids["mm"], ids["mash_t"], "Mashing",
        started_at=D(17, 8), parent_id=b2.id,
    )
    assert m2.id is not None


def test_overlapping_mashing_refused_on_exclusive_mixer(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    create_event_frame(
        session, ids["mm"], ids["mash_t"], "Mashing",
        started_at=D(17, 6), ended_at=D(17, 7, 30), parent_id=b1.id,
    )
    b2 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #2", started_at=D(17, 7)
    )
    # 7:00 collides with the mixer's 6:00-7:30 mashing
    with pytest.raises(ConflictError):
        create_event_frame(
            session, ids["mm"], ids["mash_t"], "Mashing",
            started_at=D(17, 7), parent_id=b2.id,
        )


def test_touching_windows_allowed(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    create_event_frame(
        session, ids["mm"], ids["mash_t"], "M1",
        started_at=D(17, 6), ended_at=D(17, 7, 30), parent_id=b1.id,
    )
    b2 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #2",
        started_at=D(17, 7, 30)
    )
    # starts exactly when the previous ended -- half-open, so fine
    create_event_frame(
        session, ids["mm"], ids["mash_t"], "M2",
        started_at=D(17, 7, 30), parent_id=b2.id,
    )


def test_open_frame_blocks_exclusive_element(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    create_event_frame(
        session, ids["mm"], ids["mash_t"], "M1",
        started_at=D(17, 6), parent_id=b1.id,
    )
    b2 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #2", started_at=D(17, 9)
    )
    # the first mashing is still running -> +infinity blocks everything
    with pytest.raises(ConflictError):
        create_event_frame(
            session, ids["mm"], ids["mash_t"], "M2",
            started_at=D(17, 9), parent_id=b2.id,
        )


# -- structural rules ------------------------------------------------------

def test_element_must_instance_the_template(ctx):
    session, ids = ctx
    # Brew is defined on the Brewhouse; MM1 is a Mash Mixer
    with pytest.raises(ValidationError):
        create_event_frame(
            session, ids["mm"], ids["brew_t"], "Bogus",
            started_at=D(17, 6),
        )


def test_child_requires_parent_frame(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_event_frame(
            session, ids["mm"], ids["mash_t"], "Mashing",
            started_at=D(17, 6),
        )


def test_top_level_rejects_parent(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    with pytest.raises(ValidationError):
        create_event_frame(
            session, ids["bh"], ids["brew_t"], "Brew #2",
            started_at=D(17, 7), parent_id=b1.id,
        )


def test_child_must_start_within_parent(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    with pytest.raises(ValidationError):
        create_event_frame(
            session, ids["mm"], ids["mash_t"], "Mashing",
            started_at=D(17, 5), parent_id=b1.id,
        )


def test_ended_before_started_rejected(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_event_frame(
            session, ids["bh"], ids["brew_t"], "Bad",
            started_at=D(17, 8), ended_at=D(17, 7),
        )


def test_unknown_element(ctx):
    session, ids = ctx
    with pytest.raises(NotFoundError):
        create_event_frame(
            session, 9999, ids["brew_t"], "X", started_at=D(17, 6)
        )


# -- boundary values -------------------------------------------------------

def test_open_and_close_write_default_values(ctx):
    session, ids = ctx
    status = create_lookup(session, ids["ent"], "FV Status")
    ready = create_lookup_value(session, status.id, "Ready to fill")
    empty = create_lookup_value(session, status.id, "Empty")
    create_event_frame_attribute_template(
        session, ids["brew_t"], "Status", lookup_id=status.id,
        default_start_lookup_value_id=ready.id,
        default_end_lookup_value_id=empty.id,
    )
    frame = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    readings = session.scalars(select(TagValue)).all()
    assert len(readings) == 1
    assert readings[0].lookup_value_id == ready.id
    assert readings[0].observed_at == D(17, 6)
    close_event_frame(session, frame.id, ended_at=D(17, 14))
    readings = session.scalars(
        select(TagValue).order_by(TagValue.observed_at)
    ).all()
    assert len(readings) == 2
    assert readings[1].lookup_value_id == empty.id
    assert readings[1].observed_at == D(17, 14)
    # ...and they landed on the element's wired tag
    tag = session.get(Tag, readings[0].tag_id)
    assert tag.name == "BH1.Status"


def test_numeric_defaults_written(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(
        session, ids["brew_t"], "Gravity",
        default_start_value=12.5, default_end_value=2.5,
    )
    frame = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    close_event_frame(session, frame.id, ended_at=D(17, 14))
    values = sorted(
        r.value for r in session.scalars(select(TagValue)).all()
    )
    assert values == [2.5, 12.5]


def test_attribute_without_defaults_writes_nothing(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(session, ids["brew_t"], "Notes")
    frame = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    close_event_frame(session, frame.id, ended_at=D(17, 14))
    assert session.scalars(select(TagValue)).all() == []


# -- close / reopen --------------------------------------------------------

def test_close_closes_open_descendants(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    m1 = create_event_frame(
        session, ids["mm"], ids["mash_t"], "Mashing",
        started_at=D(17, 6), parent_id=b1.id,
    )
    close_event_frame(session, b1.id, ended_at=D(17, 14))
    assert get_event_frame(session, b1.id).ended_at == D(17, 14)
    # the still-open child was closed at the same instant
    assert get_event_frame(session, m1.id).ended_at == D(17, 14)


def test_close_already_closed_rejected(ctx):
    session, ids = ctx
    frame = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1",
        started_at=D(17, 6), ended_at=D(17, 14),
    )
    with pytest.raises(ValidationError):
        close_event_frame(session, frame.id)


def test_reopen_clears_end_and_keeps_readings(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(
        session, ids["brew_t"], "Gravity",
        default_start_value=12.5, default_end_value=2.5,
    )
    frame = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    close_event_frame(session, frame.id, ended_at=D(17, 14))
    before = len(session.scalars(select(TagValue)).all())
    reopen_event_frame(session, frame.id)
    assert get_event_frame(session, frame.id).ended_at is None
    # the value written at close stays put -- operator's to correct
    assert len(session.scalars(select(TagValue)).all()) == before


def test_reopen_refused_when_it_would_overlap(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    m1 = create_event_frame(
        session, ids["mm"], ids["mash_t"], "M1",
        started_at=D(17, 6), ended_at=D(17, 7, 30), parent_id=b1.id,
    )
    b2 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #2", started_at=D(17, 8)
    )
    create_event_frame(
        session, ids["mm"], ids["mash_t"], "M2",
        started_at=D(17, 8), parent_id=b2.id,
    )
    # reopening M1 would run it to +infinity, colliding with M2
    with pytest.raises(ConflictError):
        reopen_event_frame(session, m1.id)


def test_reopen_open_frame_rejected(ctx):
    session, ids = ctx
    frame = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    with pytest.raises(ValidationError):
        reopen_event_frame(session, frame.id)


# -- update / delete -------------------------------------------------------

def test_update_end_time_correction(ctx):
    session, ids = ctx
    frame = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1",
        started_at=D(17, 6), ended_at=D(17, 14),
    )
    update_event_frame(session, frame.id, ended_at=D(17, 15))
    assert get_event_frame(session, frame.id).ended_at == D(17, 15)


def test_update_shrink_leaves_readings(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(
        session, ids["brew_t"], "Gravity",
        default_start_value=12.5, default_end_value=2.5,
    )
    frame = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1",
        started_at=D(17, 6), ended_at=D(17, 14),
    )
    before = len(session.scalars(select(TagValue)).all())
    # shrink so the end value now sits outside the window -- allowed
    update_event_frame(session, frame.id, ended_at=D(17, 10))
    assert len(session.scalars(select(TagValue)).all()) == before


def test_update_refused_when_child_would_not_fit(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    create_event_frame(
        session, ids["mm"], ids["mash_t"], "M1",
        started_at=D(17, 6), ended_at=D(17, 7, 30), parent_id=b1.id,
    )
    with pytest.raises(ValidationError):
        update_event_frame(session, b1.id, ended_at=D(17, 7))


def test_delete_frame_keeps_readings_and_tags(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(
        session, ids["brew_t"], "Gravity", default_start_value=12.5
    )
    frame = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    delete_event_frame(session, frame.id)
    assert list_event_frames(session) == []
    # the boundary reading and its tag survive
    assert len(session.scalars(select(TagValue)).all()) == 1
    assert len(session.scalars(select(Tag)).all()) == 1


def test_delete_cascades_children(ctx):
    session, ids = ctx
    b1 = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    create_event_frame(
        session, ids["mm"], ids["mash_t"], "M1",
        started_at=D(17, 6), parent_id=b1.id,
    )
    delete_event_frame(session, b1.id)
    assert list_event_frames(session) == []


def test_list_open_only(ctx):
    session, ids = ctx
    create_event_frame(
        session, ids["bh"], ids["brew_t"], "Closed",
        started_at=D(17, 6), ended_at=D(17, 7),
    )
    create_event_frame(
        session, ids["bh"], ids["brew_t"], "Running", started_at=D(17, 8)
    )
    running = list_event_frames(session, open_only=True)
    assert [f.name for f in running] == ["Running"]


def test_lookup_value_defaults_reference_survives(ctx):
    """Boundary readings hold a real lookup_value_id, not a copy."""
    session, ids = ctx
    status = create_lookup(session, ids["ent"], "FV Status")
    ready = create_lookup_value(session, status.id, "Ready to fill")
    create_event_frame_attribute_template(
        session, ids["brew_t"], "Status", lookup_id=status.id,
        default_start_lookup_value_id=ready.id,
    )
    create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    reading = session.scalars(select(TagValue)).one()
    assert session.get(LookupValue, reading.lookup_value_id).name == \
        "Ready to fill"
