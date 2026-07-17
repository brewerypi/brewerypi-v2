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


def test_create_nested_follows_a1(ctx):
    session, ids = ctx
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    mashing = create_event_frame_template(
        session, ids["mm"], "Mashing", parent_id=brew.id
    )
    assert mashing.parent_id == brew.id


def test_a1_violation_rejected(ctx):
    session, ids = ctx
    # Brew is on Brewhouse; a child whose element template is the Fermenter
    # (not a child of Brewhouse) violates the mirror
    brew = create_event_frame_template(session, ids["bh"], "Brew")
    with pytest.raises(ValidationError):
        create_event_frame_template(
            session, ids["ferm"], "Bogus", parent_id=brew.id
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
            session, ids["mm"], "Mashing", parent_id=9999
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
        session, ids["mm"], "Mashing", parent_id=brew.id
    )
    update_event_frame_template(session, mashing.id, parent_id=None)
    assert get_event_frame_template(session, mashing.id).parent_id is None


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
        session, ids["mm"], "Mashing", parent_id=brew.id
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
