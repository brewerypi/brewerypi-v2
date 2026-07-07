"""Tests for the site admin tools on the MCP server."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Tag, TagValue


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Temp DB with one enterprise. Yields enterprise_id."""
    engine = create_engine(f"sqlite:///{tmp_path / 'site.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="BRW", name="Brewery Co")
        session.add(ent)
        session.commit()
        eid = ent.id
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return eid


def test_create_and_get_site(seeded):
    created = mcp_server.create_site(seeded, "HQ", "Headquarters")
    assert created["name"] == "Headquarters"
    assert mcp_server.get_site(created["id"])["id"] == created["id"]


def test_create_site_duplicate_error(seeded):
    mcp_server.create_site(seeded, "HQ", "Headquarters")
    assert "error" in mcp_server.create_site(seeded, "HQ", "Home")


def test_create_site_unknown_enterprise_error(seeded):
    assert "error" in mcp_server.create_site(9999, "HQ", "Headquarters")


def test_update_site(seeded):
    site = mcp_server.create_site(seeded, "HQ", "Headquarters")
    updated = mcp_server.update_site(site["id"], name="Main Plant")
    assert updated["name"] == "Main Plant"


def test_delete_site_preview_reports_subtree_counts(seeded):
    site = mcp_server.create_site(seeded, "HQ", "Headquarters")
    factory = mcp_server._Session
    with factory() as session:
        area = Area(abbreviation="A", name="Area", site_id=site["id"])
        session.add(area)
        session.flush()
        session.add(Tag(name="Mash Temp", area_id=area.id))
        session.commit()
    preview = mcp_server.delete_site(site["id"])
    assert preview.get("confirm_required") is True
    assert preview["area_count"] == 1
    assert preview["tag_count"] == 1
    done = mcp_server.delete_site(site["id"], confirm=True)
    assert done == {"deleted": site["id"]}


def test_delete_site_refused_with_readings(seeded):
    site = mcp_server.create_site(seeded, "HQ", "Headquarters")
    factory = mcp_server._Session
    with factory() as session:
        area = Area(abbreviation="A", name="Area", site_id=site["id"])
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
    result = mcp_server.delete_site(site["id"], confirm=True)
    assert "error" in result
