"""Tests for the event frame template service functions."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Enterprise, Site
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_element_template,
    create_event_frame_template,
    delete_event_frame_template,
    get_event_frame_template,
    list_event_frame_templates,
    update_event_frame_template,
)


@pytest.fixture
def ctx():
    """A Brewhouse element template with a Mash Mixer child; a standalone
    Fermenter template. Yields (session, ids)."""
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
        bh = create_element_template(session, site.id, "Brewhouse")
        mm = create_element_template(
            session, site.id, "Mash Mixer", parent_id=bh.id
        )
        ferm = create_element_template(session, site.id, "Fermenter")
        yield session, {"bh": bh.id, "mm": mm.id, "ferm": ferm.id}


def test_create_top_level(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    assert brew.id is not None
    assert brew.parent_id is None
    assert brew.step_order == 1


def test_create_nested_follows_a1(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    mashing = create_event_frame_template(
        session, ids["mm"], "Mashing", step_order=1, parent_id=brew.id
    )
    assert mashing.parent_id == brew.id


def test_a1_violation_rejected(ctx):
    session, ids = ctx
    # Brew is on Brewhouse; a child whose element template is the Fermenter
    # (not a child of Brewhouse) violates the mirror
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    with pytest.raises(ValidationError):
        create_event_frame_template(
            session, ids["ferm"], "Bogus", step_order=1, parent_id=brew.id
        )


def test_top_level_on_any_element_template(ctx):
    session, ids = ctx
    # a top-level event frame template may sit on a non-top-level element
    # template (Fermentation directly on the Fermenter)
    ferm_ef = create_event_frame_template(
        session, ids["ferm"], "Fermentation"
    )
    assert ferm_ef.parent_id is None


def test_create_unknown_parent(ctx):
    session, ids = ctx
    with pytest.raises(NotFoundError):
        create_event_frame_template(
            session, ids["mm"], "Mashing", step_order=1, parent_id=9999
        )


def test_duplicate_name_per_element_template(ctx):
    session, ids = ctx
    create_event_frame_template(session, ids["bh"], "Brew")
    with pytest.raises(ConflictError):
        create_event_frame_template(session, ids["bh"], "Brew")


def test_same_name_on_different_element_templates_ok(ctx):
    session, ids = ctx
    create_event_frame_template(session, ids["bh"], "Cleaning")
    # a different element template may reuse the name
    create_event_frame_template(session, ids["ferm"], "Cleaning")


def test_update_rename_and_reparent(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    mashing = create_event_frame_template(session, ids["mm"], "Mashing")
    assert mashing.parent_id is None
    update_event_frame_template(session, mashing.id, parent_id=brew.id)
    assert get_event_frame_template(session, mashing.id).parent_id == \
        brew.id
    update_event_frame_template(session, mashing.id, name="Mash In")
    assert get_event_frame_template(session, mashing.id).name == "Mash In"


def test_update_make_top_level(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    mashing = create_event_frame_template(
        session, ids["mm"], "Mashing", step_order=2, parent_id=brew.id
    )
    update_event_frame_template(session, mashing.id, parent_id=None)
    promoted = get_event_frame_template(session, mashing.id)
    assert promoted.parent_id is None
    # promoting resets the order: a process-level template is step 1
    assert promoted.step_order == 1


def test_update_reparent_a1_violation(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    ferm_ef = create_event_frame_template(session, ids["ferm"], "Ferm")
    # Fermenter isn't a child of Brewhouse -> can't nest under Brew
    with pytest.raises(ValidationError):
        update_event_frame_template(
            session, ferm_ef.id, parent_id=brew.id
        )


def test_delete_leaf(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    delete_event_frame_template(session, brew.id)
    with pytest.raises(NotFoundError):
        get_event_frame_template(session, brew.id)


def test_delete_refused_with_children(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    create_event_frame_template(
        session, ids["mm"], "Mashing", step_order=1, parent_id=brew.id
    )
    with pytest.raises(ValidationError):
        delete_event_frame_template(session, brew.id)


def test_list_filters(ctx):
    session, ids = ctx
    create_event_frame_template(session, ids["bh"], "Brew")
    create_event_frame_template(session, ids["bh"], "Cleaning")
    rows = list_event_frame_templates(
        session, element_template_id=ids["bh"]
    )
    assert {r.name for r in rows} == {"Brew", "Cleaning"}


def test_nesting_requires_a_step_order(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    with pytest.raises(ValidationError):
        create_event_frame_template(
            session, ids["mm"], "Mashing", parent_id=brew.id
        )


def test_top_level_step_order_must_be_one(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_event_frame_template(
            session, ids["bh"], "Brew", step_order=2
        )


def test_step_order_below_one_refused(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    with pytest.raises(ValidationError):
        create_event_frame_template(
            session, ids["mm"], "Mashing", step_order=0, parent_id=brew.id
        )


def test_duplicate_step_order_under_one_parent_refused(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    create_event_frame_template(
        session, ids["mm"], "Mashing", step_order=1, parent_id=brew.id
    )
    with pytest.raises(ConflictError):
        create_event_frame_template(
            session, ids["mm"], "Lautering", step_order=1, parent_id=brew.id
        )


def test_same_step_order_under_different_parents_ok(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    cip = create_event_frame_template(session, ids["bh"], "CIP")
    create_event_frame_template(
        session, ids["mm"], "Mashing", step_order=1, parent_id=brew.id
    )
    # step 1 of the CIP is a different slot from step 1 of the Brew
    rinse = create_event_frame_template(
        session, ids["mm"], "Rinse", step_order=1, parent_id=cip.id
    )
    assert rinse.step_order == 1


def test_top_level_templates_all_hold_step_one(ctx):
    session, ids = ctx
    # two processes on the same equipment: neither is a step inside the
    # other, so both are 1 and there is nothing to collide
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    cip = create_event_frame_template(session, ids["bh"], "CIP")
    assert (brew.step_order, cip.step_order) == (1, 1)


def test_update_step_order(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    mashing = create_event_frame_template(
        session, ids["mm"], "Mashing", step_order=1, parent_id=brew.id
    )
    create_event_frame_template(
        session, ids["mm"], "Lautering", step_order=2, parent_id=brew.id
    )
    with pytest.raises(ConflictError):
        update_event_frame_template(session, mashing.id, step_order=2)
    update_event_frame_template(session, mashing.id, step_order=3)
    assert get_event_frame_template(session, mashing.id).step_order == 3


def test_update_keeps_step_order_when_renaming(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    mashing = create_event_frame_template(
        session, ids["mm"], "Mashing", step_order=2, parent_id=brew.id
    )
    update_event_frame_template(session, mashing.id, name="Mash In")
    assert get_event_frame_template(session, mashing.id).step_order == 2


def test_reparent_into_a_taken_step_refused(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    cip = create_event_frame_template(session, ids["bh"], "CIP")
    create_event_frame_template(
        session, ids["mm"], "Mashing", step_order=1, parent_id=brew.id
    )
    rinse = create_event_frame_template(
        session, ids["mm"], "Rinse", step_order=1, parent_id=cip.id
    )
    # moving Rinse under Brew would put it in Mashing's slot
    with pytest.raises(ConflictError):
        update_event_frame_template(session, rinse.id, parent_id=brew.id)
    # ...unless it is given a free one
    update_event_frame_template(
        session, rinse.id, parent_id=brew.id, step_order=2
    )
    moved = get_event_frame_template(session, rinse.id)
    assert (moved.parent_id, moved.step_order) == (brew.id, 2)


def test_list_children_in_step_order(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    create_event_frame_template(
        session, ids["mm"], "Mashing", step_order=1, parent_id=brew.id
    )
    create_event_frame_template(
        session, ids["mm"], "Boil", step_order=3, parent_id=brew.id
    )
    create_event_frame_template(
        session, ids["mm"], "Lautering", step_order=2, parent_id=brew.id
    )
    rows = list_event_frame_templates(session, parent_id=brew.id)
    # process order, not alphabetical
    assert [r.name for r in rows] == ["Mashing", "Lautering", "Boil"]
