"""Tests for the tag admin tools on the MCP server."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Enterprise,
    MeasurementUnit,
    Site,
    TagValue,
)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Temp DB: enterprise -> site -> area, plus a unit. Yields ids."""
    engine = create_engine(f"sqlite:///{tmp_path / 'tag.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="BRW", name="Brewery Co")
        session.add(ent)
        session.flush()
        site = Site(abbreviation="HQ", name="HQ", enterprise_id=ent.id)
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        unit = MeasurementUnit(
            enterprise_id=ent.id, abbreviation="°C", name="Celsius"
        )
        session.add_all([area, unit])
        session.commit()
        ids = {"area_id": area.id, "unit_id": unit.id}
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def test_create_and_get_tag(seeded):
    created = mcp_server.create_tag(
        seeded["area_id"], "Mash Temp",
        measurement_unit_id=seeded["unit_id"],
    )
    assert created["name"] == "Mash Temp"
    assert created["measurement_unit_id"] == seeded["unit_id"]
    fetched = mcp_server.get_tag(created["id"])
    assert fetched["id"] == created["id"]


def test_create_tag_both_types_error(seeded):
    result = mcp_server.create_tag(
        seeded["area_id"], "X",
        lookup_id=1, measurement_unit_id=seeded["unit_id"],
    )
    assert "error" in result


def test_create_tag_unknown_area_error(seeded):
    assert "error" in mcp_server.create_tag(9999, "X")


def test_update_tag(seeded):
    tag = mcp_server.create_tag(seeded["area_id"], "Mash Temp")
    updated = mcp_server.update_tag(tag["id"], description="the mash tun")
    assert updated["description"] == "the mash tun"


def test_get_unknown_tag_error(seeded):
    assert "error" in mcp_server.get_tag(9999)


def test_delete_tag_requires_confirm(seeded):
    tag = mcp_server.create_tag(seeded["area_id"], "Mash Temp")
    preview = mcp_server.delete_tag(tag["id"])
    assert preview.get("confirm_required") is True
    done = mcp_server.delete_tag(tag["id"], confirm=True)
    assert done == {"deleted": tag["id"]}
    assert "error" in mcp_server.get_tag(tag["id"])


def test_delete_tag_refused_with_readings(seeded):
    tag = mcp_server.create_tag(
        seeded["area_id"], "Mash Temp",
        measurement_unit_id=seeded["unit_id"],
    )
    factory = mcp_server._Session
    with factory() as session:
        session.add(
            TagValue(
                tag_id=tag["id"],
                timestamp=datetime.datetime(2026, 6, 1, 8, 0, 0),
                value=64.0,
            )
        )
        session.commit()
    result = mcp_server.delete_tag(tag["id"], confirm=True)
    assert "error" in result
