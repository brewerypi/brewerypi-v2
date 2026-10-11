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
    link_transfer,
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
            session, mm_t.id, "Mashing", step_order=1, parent_id=brew_t.id
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


# -- flow: transfers between batches ---------------------------------------
#
# A frame carrying a source and/or destination IS the transfer. The rule
# under test throughout is containment at both ends: the transfer's window
# must sit inside the window of each batch it moves beer between.

def knockout(session, ids, source, destination=None, start=None, end=None):
    """A knockout on the brewhouse, moving wort out of a brew."""
    return create_event_frame(
        session, ids["bh1"], ids["knockout_t"], "Knockout",
        started_at=start or D(17, 8),
        ended_at=end,
        source_event_frame_id=source,
        destination_event_frame_id=destination,
    )


@pytest.fixture
def brew_and_ferm(flow_ctx):
    """A brew running from 06:00 and a fermentation opened at 05:00.

    The fermentation opens *before* the knockout starts, which is the normal
    order: the vessel is claimed and ready before wort arrives.
    """
    session, ids = flow_ctx
    brew = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    ferm = create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 B123",
        started_at=D(17, 5),
    )
    return session, ids, brew, ferm


def test_knockout_links_a_brew_to_a_fermentation(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    ko = knockout(session, ids, brew.id, ferm.id, end=D(17, 9, 30))
    assert (ko.source_event_frame_id, ko.destination_event_frame_id) == (
        brew.id, ferm.id
    )


def test_two_brews_into_one_fermentation(brew_and_ferm):
    """Multiplicity is the number of transfers, not a join table."""
    session, ids, brew1, ferm = brew_and_ferm
    brew2 = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew #2", started_at=D(17, 7)
    )
    knockout(session, ids, brew1.id, ferm.id, end=D(17, 9, 30))
    knockout(
        session, ids, brew2.id, ferm.id,
        start=D(17, 10), end=D(17, 11, 30),
    )
    inbound = list_event_frames(session, destination_event_frame_id=ferm.id)
    assert len(inbound) == 2


def test_transfer_may_not_start_before_its_source(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    with pytest.raises(ValidationError, match="starts before its source"):
        knockout(session, ids, brew.id, ferm.id, start=D(17, 5, 30))


def test_transfer_after_the_source_closed_refused(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    close_event_frame(session, brew.id, ended_at=D(17, 9))
    with pytest.raises(ValidationError, match="ends after its source"):
        knockout(
            session, ids, brew.id, ferm.id,
            start=D(17, 10), end=D(17, 11),
        )


def test_open_transfer_needs_an_open_source(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    close_event_frame(session, brew.id, ended_at=D(17, 9))
    with pytest.raises(ValidationError, match="cannot be left open"):
        knockout(session, ids, brew.id, ferm.id, start=D(17, 8))


def test_packaging_run_needs_no_destination(flow_ctx):
    session, ids = flow_ctx
    cond = create_event_frame(
        session, ids["bbt01"], ids["cond_t"], "BBT01 B123",
        started_at=D(24, 6),
    )
    run = create_event_frame(
        session, ids["cl01"], ids["can_t"], "Canning B123",
        started_at=D(26, 8), source_event_frame_id=cond.id,
    )
    assert run.destination_event_frame_id is None


def test_a_batch_cannot_be_its_own_source(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    ko = knockout(session, ids, brew.id, ferm.id, end=D(17, 9, 30))
    with pytest.raises(ValidationError, match="its own transfer source"):
        link_transfer(session, ko.id, source_event_frame_id=ko.id)


def test_source_and_destination_must_differ(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    with pytest.raises(ValidationError, match="must be different batches"):
        knockout(session, ids, brew.id, brew.id, end=D(17, 9, 30))


def test_endpoint_at_another_site_refused(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    other = Site(
        abbreviation="S2", name="Site 2",
        enterprise_id=ids["ent"], timezone="UTC",
    )
    session.add(other)
    session.flush()
    far_t = create_element_template(session, other.id, "Fermenter")
    far_ferm_t = create_event_frame_template(session, far_t.id, "Ferm")
    far = create_element(session, far_t.id, "FV99")
    far_frame = create_event_frame(
        session, far.id, far_ferm_t.id, "FV99 B1", started_at=D(17, 5)
    )
    with pytest.raises(ValidationError, match="another site"):
        knockout(session, ids, brew.id, far_frame.id, end=D(17, 9, 30))


def test_source_cannot_close_while_the_transfer_runs(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    knockout(session, ids, brew.id, ferm.id)  # left open
    with pytest.raises(ConflictError, match="still running out of"):
        close_event_frame(session, brew.id, ended_at=D(17, 9, 30))


def test_destination_cannot_close_while_the_transfer_runs(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    knockout(session, ids, brew.id, ferm.id)  # left open
    with pytest.raises(ConflictError, match="still running into"):
        close_event_frame(session, ferm.id, ended_at=D(17, 9, 30))


def test_close_the_transfer_then_the_brew(brew_and_ferm):
    """The physical order: wort stops moving, then the brew day ends."""
    session, ids, brew, ferm = brew_and_ferm
    ko = knockout(session, ids, brew.id, ferm.id)
    close_event_frame(session, ko.id, ended_at=D(17, 9, 30))
    close_event_frame(session, brew.id, ended_at=D(17, 9, 30))
    assert get_event_frame(session, brew.id).ended_at == D(17, 9, 30)


def test_brew_cannot_close_before_the_transfer_finished(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    knockout(session, ids, brew.id, ferm.id, end=D(17, 9, 30))
    with pytest.raises(ValidationError, match="past the new end"):
        close_event_frame(session, brew.id, ended_at=D(17, 9))


def test_moving_an_endpoint_start_past_the_transfer_refused(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    knockout(session, ids, brew.id, ferm.id, end=D(17, 9, 30))
    with pytest.raises(ValidationError, match="before the new start"):
        update_event_frame(session, ferm.id, started_at=D(17, 8, 30))


def test_moving_the_transfer_outside_its_source_refused(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    ko = knockout(session, ids, brew.id, ferm.id, end=D(17, 9, 30))
    close_event_frame(session, brew.id, ended_at=D(17, 9, 30))
    with pytest.raises(ValidationError, match="ends after its source"):
        update_event_frame(session, ko.id, ended_at=D(17, 10))


def test_reopening_a_transfer_needs_open_endpoints(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    ko = knockout(session, ids, brew.id, ferm.id, end=D(17, 9, 30))
    close_event_frame(session, brew.id, ended_at=D(17, 9, 30))
    with pytest.raises(ValidationError, match="cannot be left open"):
        reopen_event_frame(session, ko.id)


def test_destination_is_set_late(flow_ctx):
    """The receiving batch may not exist when the beer starts moving."""
    session, ids = flow_ctx
    ferm = create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 B123", started_at=D(24, 6)
    )
    cent = create_event_frame(
        session, ids["cf01"], ids["cent_t"], "Centrifugation",
        started_at=D(24, 8), source_event_frame_id=ferm.id,
    )
    assert cent.destination_event_frame_id is None
    cond = create_event_frame(
        session, ids["bbt01"], ids["cond_t"], "BBT01 B123",
        started_at=D(24, 7),
    )
    linked = link_transfer(
        session, cent.id, destination_event_frame_id=cond.id
    )
    assert linked.destination_event_frame_id == cond.id
    assert linked.source_event_frame_id == ferm.id


def test_link_transfer_clears_a_link(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    ko = knockout(session, ids, brew.id, ferm.id, end=D(17, 9, 30))
    cleared = link_transfer(session, ko.id, destination_event_frame_id=None)
    assert cleared.destination_event_frame_id is None
    assert cleared.source_event_frame_id == brew.id


def test_link_transfer_rejects_a_late_opened_destination(flow_ctx):
    session, ids = flow_ctx
    create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 B123", started_at=D(24, 6)
    )
    cent = create_event_frame(
        session, ids["cf01"], ids["cent_t"], "Centrifugation",
        started_at=D(24, 8), ended_at=D(24, 10),
    )
    late = create_event_frame(
        session, ids["bbt01"], ids["cond_t"], "BBT01 B123",
        started_at=D(24, 9),  # opened after the transfer began
    )
    with pytest.raises(
        ValidationError, match="starts before its destination"
    ):
        link_transfer(session, cent.id, destination_event_frame_id=late.id)


def test_deleting_a_linked_batch_is_refused(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    ko = knockout(session, ids, brew.id, ferm.id, end=D(17, 9, 30))
    with pytest.raises(ConflictError, match="unlink the transfer first"):
        delete_event_frame(session, brew.id)
    link_transfer(session, ko.id, source_event_frame_id=None)
    delete_event_frame(session, brew.id)
    assert get_event_frame(session, ko.id).source_event_frame_id is None


def test_deleting_the_transfer_itself_is_allowed(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    ko = knockout(session, ids, brew.id, ferm.id, end=D(17, 9, 30))
    delete_event_frame(session, ko.id)
    assert get_event_frame(session, brew.id).id == brew.id


def test_deleting_a_parent_refused_when_a_child_is_linked(ctx):
    """The guard looks at the whole subtree a delete would take with it."""
    session, ids = ctx
    brew = create_event_frame(
        session, ids["bh"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    mash = create_event_frame(
        session, ids["mm"], ids["mash_t"], "Mashing",
        started_at=D(17, 6), parent_id=brew.id,
    )
    create_event_frame(
        session, ids["bh"], ids["brew_t"], "Wort transfer",
        started_at=D(17, 7), source_event_frame_id=mash.id,
    )
    with pytest.raises(ConflictError, match="unlink the transfer first"):
        delete_event_frame(session, brew.id)


def test_list_filters_by_equipment_type(flow_ctx):
    """How a picker is populated: every open fermentation, by vessel."""
    session, ids = flow_ctx
    create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 B123", started_at=D(17, 5)
    )
    create_event_frame(
        session, ids["fv02"], ids["ferm_t"], "FV02 B124", started_at=D(17, 6)
    )
    create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    found = list_event_frames(
        session, element_template_id=ids["fv_t"], open_only=True
    )
    assert {f.name for f in found} == {"FV01 B123", "FV02 B124"}


# -- flow: transfers that are steps inside a batch -------------------------
#
# A wort cooling is a step of the brew it empties, so its template's
# parent_flow names the brew as its source; a yeast pitch is a step of the
# wort cooling it doses, so its parent is its destination. Only the other
# end is ever chosen.


def wort_cooling(session, ids, brew_id, start=None, **kwargs):
    return create_event_frame(
        session, ids["wc1"], ids["cool_t"], "Wort cooling",
        started_at=start or D(17, 8), parent_id=brew_id, **kwargs,
    )


def test_wort_cooling_draws_from_the_brew_it_sits_inside(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    cooling = wort_cooling(
        session, ids, brew.id, destination_event_frame_id=ferm.id
    )
    assert cooling.source_event_frame_id == brew.id
    assert cooling.destination_event_frame_id == ferm.id


def test_wort_cooling_has_its_source_before_a_destination_exists(
    brew_and_ferm,
):
    session, ids, brew, _ = brew_and_ferm
    cooling = wort_cooling(session, ids, brew.id)
    assert cooling.source_event_frame_id == brew.id
    assert cooling.destination_event_frame_id is None


def test_yeast_pitch_delivers_into_its_wort_cooling(brew_and_ferm):
    """Only the yeast's source is chosen; where it goes is the nesting."""
    session, ids, brew, ferm = brew_and_ferm
    yeast = create_event_frame(
        session, ids["fv02"], ids["ferm_t"], "FV02 Prop", started_at=D(16, 5)
    )
    cooling = wort_cooling(
        session, ids, brew.id, destination_event_frame_id=ferm.id
    )
    pitch = create_event_frame(
        session, ids["yd1"], ids["pitch_t"], "Yeast pitch",
        started_at=D(17, 8, 10), parent_id=cooling.id,
        source_event_frame_id=yeast.id,
    )
    assert pitch.source_event_frame_id == yeast.id
    assert pitch.destination_event_frame_id == cooling.id


def test_an_implied_end_cannot_name_another_batch(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    with pytest.raises(ValidationError, match="always the batch it sits"):
        wort_cooling(session, ids, brew.id, source_event_frame_id=ferm.id)


def test_an_implied_end_cannot_be_cleared_or_moved(brew_and_ferm):
    session, ids, brew, ferm = brew_and_ferm
    cooling = wort_cooling(session, ids, brew.id)
    with pytest.raises(ValidationError, match="cannot be cleared"):
        link_transfer(session, cooling.id, source_event_frame_id=None)
    with pytest.raises(ValidationError, match="always the batch it sits"):
        link_transfer(session, cooling.id, source_event_frame_id=ferm.id)
    # the chosen end is still free to set and clear
    link_transfer(session, cooling.id, destination_event_frame_id=ferm.id)
    cleared = link_transfer(
        session, cooling.id, destination_event_frame_id=None
    )
    assert cleared.source_event_frame_id == brew.id
    assert cleared.destination_event_frame_id is None


def test_linking_fills_the_implied_end_on_an_older_frame(brew_and_ferm):
    """A frame recorded before its template had a parent_flow."""
    session, ids, brew, ferm = brew_and_ferm
    cooling = wort_cooling(session, ids, brew.id)
    cooling.source_event_frame_id = None
    session.flush()
    linked = link_transfer(
        session, cooling.id, destination_event_frame_id=ferm.id
    )
    assert linked.source_event_frame_id == brew.id


def test_closing_the_brew_closes_the_transfers_inside_it(brew_and_ferm):
    """The step is linked to the brew AND nested in it; nesting wins.

    A separate transfer still running out of a brew blocks its close. A
    step of the brew cannot, or no brew with a wort cooling could ever be
    closed in one action: it ends at the same instant, so it still fits.
    """
    session, ids, brew, ferm = brew_and_ferm
    yeast = create_event_frame(
        session, ids["fv02"], ids["ferm_t"], "FV02 Prop", started_at=D(16, 5)
    )
    cooling = wort_cooling(
        session, ids, brew.id, destination_event_frame_id=ferm.id
    )
    pitch = create_event_frame(
        session, ids["yd1"], ids["pitch_t"], "Yeast pitch",
        started_at=D(17, 8, 10), parent_id=cooling.id,
        source_event_frame_id=yeast.id,
    )
    close_event_frame(session, brew.id, ended_at=D(17, 10))
    assert brew.ended_at == cooling.ended_at == pitch.ended_at == D(17, 10)


def test_a_separate_transfer_blocks_even_when_steps_close_along(
    brew_and_ferm,
):
    session, ids, brew, ferm = brew_and_ferm
    wort_cooling(session, ids, brew.id, destination_event_frame_id=ferm.id)
    knockout(session, ids, brew.id, ferm.id, start=D(17, 9))
    with pytest.raises(ConflictError, match="still running out of"):
        close_event_frame(session, brew.id, ended_at=D(17, 10))


def test_one_brew_cools_into_a_fermentation_and_a_propagation(
    brew_and_ferm,
):
    """Two wort coolings in one brew, each to its own destination."""
    session, ids, brew, ferm = brew_and_ferm
    prop = create_event_frame(
        session, ids["fv02"], ids["ferm_t"], "FV02 Prop", started_at=D(17, 5)
    )
    first = wort_cooling(
        session, ids, brew.id, ended_at=D(17, 9),
        destination_event_frame_id=ferm.id,
    )
    second = wort_cooling(
        session, ids, brew.id, start=D(17, 9), ended_at=D(17, 9, 30),
        destination_event_frame_id=prop.id,
    )
    assert first.source_event_frame_id == second.source_event_frame_id
    assert second.source_event_frame_id == brew.id
    assert second.destination_event_frame_id == prop.id
