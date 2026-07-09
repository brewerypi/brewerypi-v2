"""Tests for the element attribute template admin tools."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Enterprise, Lookup, MeasurementUnit, Site
from brewerypi.services import create_element_template


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """DB with a Fermenter template + a lookup and unit. Yields ids."""
    engine = create_engine(f"sqlite:///{tmp_path / 'eat.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        site = Site(abbreviation="S", name="Site", enterprise_id=ent.id)
        session.add(site)
        session.flush()
        unit = MeasurementUnit(
            enterprise_id=ent.id, abbreviation="C", name="Celsius"
        )
        lookup = Lookup(enterprise_id=ent.id, name="Stage")
        session.add_all([unit, lookup])
        ferm = create_element_template(session, site.id, "Fermenter")
        session.commit()
        ids = {"ferm": ferm.id, "unit": unit.id, "lookup": lookup.id}
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def test_create_and_list(seeded):
    temp = mcp_server.create_element_attribute_template(
        seeded["ferm"], "Temperature", measurement_unit_id=seeded["unit"]
    )
    assert temp["measurement_unit_id"] == seeded["unit"]
    mcp_server.create_element_attribute_template(
        seeded["ferm"], "Stage", lookup_id=seeded["lookup"]
    )
    rows = mcp_server.list_element_attribute_templates(seeded["ferm"])
    assert {r["name"] for r in rows} == {"Temperature", "Stage"}


def test_create_both_types_error(seeded):
    result = mcp_server.create_element_attribute_template(
        seeded["ferm"], "X",
        lookup_id=seeded["lookup"],
        measurement_unit_id=seeded["unit"],
    )
    assert "error" in result


def test_create_unknown_element_template_error(seeded):
    assert "error" in mcp_server.create_element_attribute_template(
        9999, "Temperature"
    )


def test_update(seeded):
    temp = mcp_server.create_element_attribute_template(
        seeded["ferm"], "Temperature"
    )
    updated = mcp_server.update_element_attribute_template(
        temp["id"], description="degrees C"
    )
    assert updated["description"] == "degrees C"


def test_delete_requires_confirm(seeded):
    temp = mcp_server.create_element_attribute_template(
        seeded["ferm"], "Temperature"
    )
    preview = mcp_server.delete_element_attribute_template(temp["id"])
    assert preview.get("confirm_required") is True
    done = mcp_server.delete_element_attribute_template(
        temp["id"], confirm=True
    )
    assert done == {"deleted": temp["id"]}
    # gone from the list afterward
    remaining = mcp_server.list_element_attribute_templates(seeded["ferm"])
    assert remaining == []
