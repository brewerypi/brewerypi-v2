"""Tests for the enterprise admin tools on the MCP server."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import (
    Area,
    MeasurementUnit,
    Site,
    Tag,
    TagValue,
)


@pytest.fixture
def factory(tmp_path, monkeypatch):
    """Empty temp DB; point the server's factory at it."""
    engine = create_engine(f"sqlite:///{tmp_path / 'ent.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return sessionmaker(engine)


def test_create_and_get_enterprise(factory):
    created = mcp_server.create_enterprise("BRW", "Brewery Co")
    assert created["name"] == "Brewery Co"
    assert mcp_server.get_enterprise(created["id"])["id"] == created["id"]


def test_create_enterprise_duplicate_error(factory):
    mcp_server.create_enterprise("BRW", "Brewery Co")
    assert "error" in mcp_server.create_enterprise("BRW", "Other Co")


def test_update_enterprise(factory):
    ent = mcp_server.create_enterprise("BRW", "Brewery Co")
    updated = mcp_server.update_enterprise(ent["id"], name="Brewery Company")
    assert updated["name"] == "Brewery Company"


def test_get_unknown_enterprise_error(factory):
    assert "error" in mcp_server.get_enterprise(9999)


def test_delete_preview_reports_full_subtree(factory):
    ent = mcp_server.create_enterprise("BRW", "Brewery Co")
    with factory() as session:
        site = Site(
            abbreviation="HQ", name="HQ", enterprise_id=ent["id"]
        )
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        session.add(area)
        session.flush()
        session.add(Tag(name="Mash Temp", area_id=area.id))
        session.add(
            MeasurementUnit(
                enterprise_id=ent["id"], abbreviation="°C", name="Celsius"
            )
        )
        session.commit()
    preview = mcp_server.delete_enterprise(ent["id"])
    assert preview.get("confirm_required") is True
    assert preview["site_count"] == 1
    assert preview["area_count"] == 1
    assert preview["tag_count"] == 1
    assert preview["measurement_unit_count"] == 1
    done = mcp_server.delete_enterprise(ent["id"], confirm=True)
    assert done == {"deleted": ent["id"]}


def test_delete_refused_with_readings(factory):
    ent = mcp_server.create_enterprise("BRW", "Brewery Co")
    with factory() as session:
        site = Site(
            abbreviation="HQ", name="HQ", enterprise_id=ent["id"]
        )
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        session.add(area)
        session.flush()
        tag = Tag(name="Mash Temp", area_id=area.id)
        session.add(tag)
        session.flush()
        session.add(
            TagValue(
                tag_id=tag.id,
                observed_at=datetime.datetime(2026, 6, 1, 8, 0, 0),
                value=64.0,
            )
        )
        session.commit()
    result = mcp_server.delete_enterprise(ent["id"], confirm=True)
    assert "error" in result
