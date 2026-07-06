"""Tests for the lookup/lookup-value admin tools on the MCP server."""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site, Tag, TagValue


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Temp DB with one enterprise; point the server's factory at it."""
    engine = create_engine(f"sqlite:///{tmp_path / 'lk.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="BRW", name="Brewery Co")
        session.add(ent)
        session.commit()
        eid = ent.id
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return eid


def _reference_value_with_reading(eid, lookup_id, value_id):
    """Create a lookup-typed tag + a reading that uses ``value_id``."""
    factory = mcp_server._Session
    with factory() as session:
        site = Site(abbreviation="HQ", name="HQ", enterprise_id=eid)
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        session.add(area)
        session.flush()
        tag = Tag(name="Stage", area_id=area.id, lookup_id=lookup_id)
        session.add(tag)
        session.flush()
        session.add(
            TagValue(
                tag_id=tag.id,
                timestamp=datetime.datetime(2026, 6, 1, 8, 0, 0),
                lookup_value_id=value_id,
            )
        )
        session.commit()


def test_lookup_crud_flow(seeded):
    lk = mcp_server.create_lookup(seeded, "Fermentation Stage")
    assert lk["name"] == "Fermentation Stage"
    assert mcp_server.list_lookups(seeded)[0]["id"] == lk["id"]
    renamed = mcp_server.update_lookup(lk["id"], name="Stage")
    assert renamed["name"] == "Stage"


def test_create_lookup_duplicate_error(seeded):
    mcp_server.create_lookup(seeded, "Stage")
    assert "error" in mcp_server.create_lookup(seeded, "Stage")


def test_create_lookup_unknown_enterprise_error(seeded):
    assert "error" in mcp_server.create_lookup(9999, "Stage")


def test_lookup_value_crud_flow(seeded):
    lk = mcp_server.create_lookup(seeded, "Stage")
    val = mcp_server.create_lookup_value(lk["id"], "Primary")
    assert val["is_selectable"] is True
    updated = mcp_server.update_lookup_value(
        val["id"], is_selectable=False
    )
    assert updated["is_selectable"] is False
    listed = mcp_server.list_lookup_values(lk["id"])
    assert [v["name"] for v in listed] == ["Primary"]


def test_create_value_unknown_lookup_error(seeded):
    assert "error" in mcp_server.create_lookup_value(9999, "Primary")


def test_delete_lookup_requires_confirm(seeded):
    lk = mcp_server.create_lookup(seeded, "Stage")
    mcp_server.create_lookup_value(lk["id"], "Primary")
    preview = mcp_server.delete_lookup(lk["id"])
    assert preview.get("confirm_required") is True
    assert len(mcp_server.list_lookups(seeded)) == 1
    done = mcp_server.delete_lookup(lk["id"], confirm=True)
    assert done == {"deleted": lk["id"]}
    assert mcp_server.list_lookups(seeded) == []


def test_delete_lookup_refused_when_reading_references_value(seeded):
    lk = mcp_server.create_lookup(seeded, "Stage")
    val = mcp_server.create_lookup_value(lk["id"], "Primary")
    _reference_value_with_reading(seeded, lk["id"], val["id"])
    assert "error" in mcp_server.delete_lookup(lk["id"], confirm=True)


def test_delete_value_refused_when_referenced(seeded):
    lk = mcp_server.create_lookup(seeded, "Stage")
    val = mcp_server.create_lookup_value(lk["id"], "Primary")
    _reference_value_with_reading(seeded, lk["id"], val["id"])
    result = mcp_server.delete_lookup_value(val["id"], confirm=True)
    assert "error" in result
