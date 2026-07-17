"""Tests for the event frame template tools: operator reads, admin writes."""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Enterprise, Site
from brewerypi.services import create_element_template


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Brewhouse element template with a Mash Mixer child; a Fermenter."""
    engine = create_engine(f"sqlite:///{tmp_path / 'eft.db'}")
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
        session.commit()
        ids = {"bh": bh.id, "mm": mm.id, "ferm": ferm.id}
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def test_create_nested_and_list(seeded):
    brew = mcp_server.create_event_frame_template(seeded["bh"], "Brew")
    assert brew["parent_id"] is None
    mashing = mcp_server.create_event_frame_template(
        seeded["mm"], "Mashing", parent_id=brew["id"]
    )
    assert mashing["parent_id"] == brew["id"]
    rows = mcp_server.list_event_frame_templates(
        element_template_id=seeded["bh"]
    )
    assert [r["name"] for r in rows] == ["Brew"]


def test_create_a1_violation_error(seeded):
    brew = mcp_server.create_event_frame_template(seeded["bh"], "Brew")
    # Fermenter isn't a child of Brewhouse
    result = mcp_server.create_event_frame_template(
        seeded["ferm"], "Bogus", parent_id=brew["id"]
    )
    assert "error" in result


def test_get(seeded):
    brew = mcp_server.create_event_frame_template(seeded["bh"], "Brew")
    assert mcp_server.get_event_frame_template(brew["id"])["name"] == "Brew"


def test_update_reparent(seeded):
    brew = mcp_server.create_event_frame_template(seeded["bh"], "Brew")
    mashing = mcp_server.create_event_frame_template(
        seeded["mm"], "Mashing"
    )
    moved = mcp_server.update_event_frame_template(
        mashing["id"], parent_id=brew["id"]
    )
    assert moved["parent_id"] == brew["id"]
    top = mcp_server.update_event_frame_template(
        mashing["id"], make_top_level=True
    )
    assert top["parent_id"] is None


def test_delete_requires_confirm_and_guards_children(seeded):
    brew = mcp_server.create_event_frame_template(seeded["bh"], "Brew")
    mcp_server.create_event_frame_template(
        seeded["mm"], "Mashing", parent_id=brew["id"]
    )
    preview = mcp_server.delete_event_frame_template(brew["id"])
    assert preview["child_count"] == 1
    assert "error" in mcp_server.delete_event_frame_template(
        brew["id"], confirm=True
    )


def test_reads_operator_writes_admin():
    async def names(server):
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    operator = asyncio.run(names(mcp_server.mcp))
    assert {
        "list_event_frame_templates",
        "get_event_frame_template",
    } <= operator
    assert not (
        {
            "create_event_frame_template",
            "update_event_frame_template",
            "delete_event_frame_template",
        }
        & operator
    )
    admin_only = FastMCP("t")
    mcp_server._register_config_tools(admin_only)
    config = asyncio.run(names(admin_only))
    assert {
        "create_event_frame_template",
        "update_event_frame_template",
        "delete_event_frame_template",
    } <= config
