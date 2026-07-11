"""Tests for name-segment rules on names used in generated tag paths."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Enterprise, Site
from brewerypi.services import (
    ValidationError,
    create_element,
    create_element_attribute_template,
    create_element_template,
    get_element,
    update_element,
)
from brewerypi.services._validation import clean_name_segment


@pytest.fixture
def ctx():
    """A site with a top-level Fermenter template. Yields (session, ids)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        site = Site(abbreviation="S", name="Site", enterprise_id=ent.id)
        session.add(site)
        session.flush()
        ferm = create_element_template(session, site.id, "Fermenter")
        yield session, {"site": site.id, "ferm": ferm.id}


# -- the helper itself -----------------------------------------------------

def test_keeps_internal_spaces():
    assert clean_name_segment("Hot Liquor Tank", "name", 45) == \
        "Hot Liquor Tank"


def test_trims_and_collapses_whitespace():
    assert clean_name_segment("  Hot   Liquor  Tank ", "name", 45) == \
        "Hot Liquor Tank"


def test_rejects_dot():
    with pytest.raises(ValidationError):
        clean_name_segment("FV.01", "name", 45)


def test_rejects_blank():
    with pytest.raises(ValidationError):
        clean_name_segment("   ", "name", 45)


def test_enforces_max_length():
    with pytest.raises(ValidationError):
        clean_name_segment("x" * 46, "name", 45)


# -- applied to element names ----------------------------------------------

def test_element_name_keeps_spaces(ctx):
    session, ids = ctx
    el = create_element(session, ids["ferm"], "  Hot   Liquor Tank ")
    assert el.name == "Hot Liquor Tank"


def test_element_name_rejects_dot(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_element(session, ids["ferm"], "FV.01")


def test_element_update_name_rejects_dot(ctx):
    session, ids = ctx
    el = create_element(session, ids["ferm"], "FV01")
    with pytest.raises(ValidationError):
        update_element(session, el.id, name="FV.01")


def test_element_update_name_collapses_spaces(ctx):
    session, ids = ctx
    el = create_element(session, ids["ferm"], "FV01")
    update_element(session, el.id, name="FV  01")
    assert get_element(session, el.id).name == "FV 01"


# -- applied to attribute template names -----------------------------------

def test_attribute_template_name_keeps_spaces(ctx):
    session, ids = ctx
    at = create_element_attribute_template(
        session, ids["ferm"], "  Set   Point "
    )
    assert at.name == "Set Point"


def test_attribute_template_name_rejects_dot(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_element_attribute_template(session, ids["ferm"], "Temp.C")
