"""Tests for the element-template service functions."""

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
    delete_element_template,
    get_element_template,
    list_element_templates,
    update_element_template,
)


@pytest.fixture
def ctx():
    """Two sites under one enterprise; yields (session, site1, site2)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        s1 = Site(abbreviation="S1", name="Site One", enterprise_id=ent.id)
        s2 = Site(abbreviation="S2", name="Site Two", enterprise_id=ent.id)
        session.add_all([s1, s2])
        session.flush()
        yield session, s1.id, s2.id


def test_create_top_level_and_get(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    assert bh.id is not None
    assert bh.parent_id is None
    assert get_element_template(session, bh.id).name == "Brewhouse"


def test_create_child(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    mt = create_element_template(
        session, s1, "Mash Tun", parent_id=bh.id
    )
    assert mt.parent_id == bh.id


def test_create_unknown_site(ctx):
    session, _, _ = ctx
    with pytest.raises(NotFoundError):
        create_element_template(session, 9999, "Brewhouse")


def test_create_unknown_parent(ctx):
    session, s1, _ = ctx
    with pytest.raises(NotFoundError):
        create_element_template(session, s1, "Mash Tun", parent_id=9999)


def test_create_parent_in_other_site(ctx):
    session, s1, s2 = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    with pytest.raises(ValidationError):
        create_element_template(session, s2, "Mash Tun", parent_id=bh.id)


def test_create_duplicate_name_in_site(ctx):
    session, s1, _ = ctx
    create_element_template(session, s1, "Brewhouse")
    with pytest.raises(ConflictError):
        create_element_template(session, s1, "Brewhouse")


def test_same_name_allowed_in_different_sites(ctx):
    session, s1, s2 = ctx
    create_element_template(session, s1, "Brewhouse")
    # not a conflict — different site
    create_element_template(session, s2, "Brewhouse")


def test_update_name_and_description(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    update_element_template(
        session, bh.id, name="Brew House", description="the hot side"
    )
    got = get_element_template(session, bh.id)
    assert got.name == "Brew House"
    assert got.description == "the hot side"


def test_update_name_conflict(ctx):
    session, s1, _ = ctx
    create_element_template(session, s1, "Brewhouse")
    other = create_element_template(session, s1, "Cellar")
    with pytest.raises(ConflictError):
        update_element_template(session, other.id, name="Brewhouse")


def test_reparent(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    cellar = create_element_template(session, s1, "Cellar")
    mt = create_element_template(session, s1, "Mash Tun", parent_id=bh.id)
    update_element_template(session, mt.id, parent_id=cellar.id)
    assert get_element_template(session, mt.id).parent_id == cellar.id


def test_promote_to_top_level(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    mt = create_element_template(session, s1, "Mash Tun", parent_id=bh.id)
    update_element_template(session, mt.id, parent_id=None)
    assert get_element_template(session, mt.id).parent_id is None


def test_update_parent_unchanged_when_omitted(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    mt = create_element_template(session, s1, "Mash Tun", parent_id=bh.id)
    update_element_template(session, mt.id, name="Mash/Lauter Tun")
    assert get_element_template(session, mt.id).parent_id == bh.id


def test_self_parent_rejected(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    with pytest.raises(ValidationError):
        update_element_template(session, bh.id, parent_id=bh.id)


def test_descendant_parent_rejected(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    mt = create_element_template(session, s1, "Mash Tun", parent_id=bh.id)
    # making Brewhouse a child of its own descendant is a cycle
    with pytest.raises(ValidationError):
        update_element_template(session, bh.id, parent_id=mt.id)


def test_reparent_to_other_site_rejected(ctx):
    session, s1, s2 = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    other = create_element_template(session, s2, "Brewhouse")
    with pytest.raises(ValidationError):
        update_element_template(session, bh.id, parent_id=other.id)


def test_list_filters_by_site(ctx):
    session, s1, s2 = ctx
    create_element_template(session, s1, "Brewhouse")
    create_element_template(session, s1, "Cellar")
    create_element_template(session, s2, "Packaging")
    names = {t.name for t in list_element_templates(session, site_id=s1)}
    assert names == {"Brewhouse", "Cellar"}


def test_delete_leaf(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    delete_element_template(session, bh.id)
    with pytest.raises(NotFoundError):
        get_element_template(session, bh.id)


def test_delete_refused_with_children(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    create_element_template(session, s1, "Mash Tun", parent_id=bh.id)
    with pytest.raises(ValidationError):
        delete_element_template(session, bh.id)


def test_exclusive_defaults_true(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    assert bh.exclusive is True


def test_create_non_exclusive(ctx):
    session, s1, _ = ctx
    bh = create_element_template(
        session, s1, "Brewhouse", exclusive=False
    )
    assert bh.exclusive is False


def test_update_toggles_exclusive(ctx):
    session, s1, _ = ctx
    bh = create_element_template(session, s1, "Brewhouse")
    update_element_template(session, bh.id, exclusive=False)
    assert get_element_template(session, bh.id).exclusive is False
    # a plain rename leaves exclusive untouched
    update_element_template(session, bh.id, name="Brew House")
    assert get_element_template(session, bh.id).exclusive is False
