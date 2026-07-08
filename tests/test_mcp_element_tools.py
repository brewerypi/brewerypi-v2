"""Tests for the element tools: operator reads, admin writes."""

import asyncio

import pytest
from fastmcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site
from brewerypi.services import create_element_template


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """DB with a Cellar>Fermenter template tree + a tag area. Yields ids."""
    engine = create_engine(f"sqlite:///{tmp_path / 'el.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        site = Site(abbreviation="S", name="Site", enterprise_id=ent.id)
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        session.add(area)
        session.flush()
        cellar_t = create_element_template(session, site.id, "Cellar")
        ferm_t = create_element_template(
            session, site.id, "Fermenter", parent_id=cellar_t.id
        )
        session.commit()
        ids = {
            "site": site.id,
            "area": area.id,
            "cellar_t": cellar_t.id,
            "ferm_t": ferm_t.id,
        }
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def test_create_tree_and_read(seeded):
    cellar = mcp_server.create_element(seeded["cellar_t"], "Cellar")
    assert cellar["parent_id"] is None
    fv01 = mcp_server.create_element(
        seeded["ferm_t"], "FV01", parent_id=cellar["id"]
    )
    assert fv01["parent_id"] == cellar["id"]
    # operator reads
    assert mcp_server.get_element(fv01["id"])["name"] == "FV01"
    ferms = mcp_server.list_elements(
        element_template_id=seeded["ferm_t"]
    )
    assert [e["name"] for e in ferms] == ["FV01"]


def test_create_violation_returns_error(seeded):
    # a child template needs a parent
    assert "error" in mcp_server.create_element(seeded["ferm_t"], "FV01")


def test_create_bad_tag_area_error(seeded):
    # area belongs to the site, but pass a nonexistent one
    assert "error" in mcp_server.create_element(
        seeded["cellar_t"], "Cellar", tag_area_id=9999
    )


def test_update_assign_and_clear_tag_area(seeded):
    cellar = mcp_server.create_element(seeded["cellar_t"], "Cellar")
    assigned = mcp_server.update_element(
        cellar["id"], tag_area_id=seeded["area"]
    )
    assert assigned["tag_area_id"] == seeded["area"]
    cleared = mcp_server.update_element(cellar["id"], clear_tag_area=True)
    assert cleared["tag_area_id"] is None


def test_update_reparent(seeded):
    c1 = mcp_server.create_element(seeded["cellar_t"], "Cellar A")
    c2 = mcp_server.create_element(seeded["cellar_t"], "Cellar B")
    fv = mcp_server.create_element(
        seeded["ferm_t"], "FV01", parent_id=c1["id"]
    )
    moved = mcp_server.update_element(fv["id"], parent_id=c2["id"])
    assert moved["parent_id"] == c2["id"]


def test_delete_requires_confirm_and_guards_children(seeded):
    cellar = mcp_server.create_element(seeded["cellar_t"], "Cellar")
    fv = mcp_server.create_element(
        seeded["ferm_t"], "FV01", parent_id=cellar["id"]
    )
    # parent has a child -> preview shows count, confirm refuses
    preview = mcp_server.delete_element(cellar["id"])
    assert preview["child_count"] == 1
    assert "error" in mcp_server.delete_element(cellar["id"], confirm=True)
    # the leaf deletes fine
    assert mcp_server.delete_element(fv["id"], confirm=True) == {
        "deleted": fv["id"]
    }


def test_read_tools_on_operator_write_tools_admin_only():
    from fastmcp import FastMCP

    async def names(server):
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    # The default server is the operator tier: reads present, writes absent.
    operator = asyncio.run(names(mcp_server.mcp))
    assert {"list_elements", "get_element"} <= operator
    assert not (
        {"create_element", "update_element", "delete_element"} & operator
    )

    # The admin config-tools helper registers the writes.
    admin_only = FastMCP("t")
    mcp_server._register_config_tools(admin_only)
    config = asyncio.run(names(admin_only))
    assert {
        "create_element",
        "update_element",
        "delete_element",
    } <= config
