"""Tests for the area admin tools on the MCP server."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Enterprise, Site, Tag, TagValue


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Temp DB: enterprise -> site. Yields site_id."""
    engine = create_engine(f"sqlite:///{tmp_path / 'area.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="BRW", name="Brewery Co")
        session.add(ent)
        session.flush()
        site = Site(abbreviation="HQ", name="HQ", enterprise_id=ent.id)
        session.add(site)
        session.commit()
        sid = site.id
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return sid


def test_create_and_get_area(seeded):
    created = mcp_server.create_area(seeded, "BH", "Brewhouse")
    assert created["name"] == "Brewhouse"
    assert mcp_server.get_area(created["id"])["id"] == created["id"]


def test_create_area_duplicate_error(seeded):
    mcp_server.create_area(seeded, "BH", "Brewhouse")
    assert "error" in mcp_server.create_area(seeded, "BH", "Bright")


def test_create_area_unknown_site_error(seeded):
    assert "error" in mcp_server.create_area(9999, "BH", "Brewhouse")


def test_update_area(seeded):
    area = mcp_server.create_area(seeded, "BH", "Brewhouse")
    updated = mcp_server.update_area(area["id"], name="Brew House")
    assert updated["name"] == "Brew House"


def test_delete_area_preview_reports_tag_count(seeded):
    area = mcp_server.create_area(seeded, "BH", "Brewhouse")
    factory = mcp_server._Session
    with factory() as session:
        session.add(Tag(name="Mash Temp", area_id=area["id"]))
        session.commit()
    preview = mcp_server.delete_area(area["id"])
    assert preview.get("confirm_required") is True
    assert preview["tag_count"] == 1
    done = mcp_server.delete_area(area["id"], confirm=True)
    assert done == {"deleted": area["id"]}


def test_delete_area_refused_with_readings(seeded):
    area = mcp_server.create_area(seeded, "BH", "Brewhouse")
    factory = mcp_server._Session
    with factory() as session:
        tag = Tag(name="Mash Temp", area_id=area["id"])
        session.add(tag)
        session.flush()
        session.add(
            TagValue(
                tag_id=tag.id,
                timestamp=datetime.datetime(2026, 6, 1, 8, 0, 0),
                value=64.0,
            )
        )
        session.commit()
    result = mcp_server.delete_area(area["id"], confirm=True)
    assert "error" in result
