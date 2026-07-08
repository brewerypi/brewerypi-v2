"""Tests for the element-template admin tools on the MCP server."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Enterprise, Site


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Temp DB: enterprise -> site. Yields site_id."""
    engine = create_engine(f"sqlite:///{tmp_path / 'et.db'}")
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


def test_create_tree_and_list(seeded):
    bh = mcp_server.create_element_template(seeded, "Brewhouse")
    assert bh["parent_id"] is None
    mt = mcp_server.create_element_template(
        seeded, "Mash Tun", parent_id=bh["id"]
    )
    assert mt["parent_id"] == bh["id"]
    rows = mcp_server.list_element_templates(seeded)
    assert {r["name"] for r in rows} == {"Brewhouse", "Mash Tun"}


def test_create_duplicate_error(seeded):
    mcp_server.create_element_template(seeded, "Brewhouse")
    assert "error" in mcp_server.create_element_template(seeded, "Brewhouse")


def test_create_bad_parent_error(seeded):
    assert "error" in mcp_server.create_element_template(
        seeded, "Mash Tun", parent_id=9999
    )


def test_update_rename(seeded):
    bh = mcp_server.create_element_template(seeded, "Brewhouse")
    updated = mcp_server.update_element_template(bh["id"], name="Brew House")
    assert updated["name"] == "Brew House"


def test_update_reparent(seeded):
    bh = mcp_server.create_element_template(seeded, "Brewhouse")
    cellar = mcp_server.create_element_template(seeded, "Cellar")
    mt = mcp_server.create_element_template(
        seeded, "Mash Tun", parent_id=bh["id"]
    )
    moved = mcp_server.update_element_template(
        mt["id"], parent_id=cellar["id"]
    )
    assert moved["parent_id"] == cellar["id"]


def test_update_promote_to_top_level(seeded):
    bh = mcp_server.create_element_template(seeded, "Brewhouse")
    mt = mcp_server.create_element_template(
        seeded, "Mash Tun", parent_id=bh["id"]
    )
    promoted = mcp_server.update_element_template(
        mt["id"], make_top_level=True
    )
    assert promoted["parent_id"] is None


def test_update_parent_unchanged_when_neither_flag(seeded):
    bh = mcp_server.create_element_template(seeded, "Brewhouse")
    mt = mcp_server.create_element_template(
        seeded, "Mash Tun", parent_id=bh["id"]
    )
    renamed = mcp_server.update_element_template(
        mt["id"], name="Mash/Lauter Tun"
    )
    assert renamed["parent_id"] == bh["id"]


def test_update_cycle_error(seeded):
    bh = mcp_server.create_element_template(seeded, "Brewhouse")
    mt = mcp_server.create_element_template(
        seeded, "Mash Tun", parent_id=bh["id"]
    )
    assert "error" in mcp_server.update_element_template(
        bh["id"], parent_id=mt["id"]
    )


def test_delete_leaf_with_confirm(seeded):
    bh = mcp_server.create_element_template(seeded, "Brewhouse")
    preview = mcp_server.delete_element_template(bh["id"])
    assert preview.get("confirm_required") is True
    assert preview["child_count"] == 0
    done = mcp_server.delete_element_template(bh["id"], confirm=True)
    assert done == {"deleted": bh["id"]}


def test_delete_refused_with_children(seeded):
    bh = mcp_server.create_element_template(seeded, "Brewhouse")
    mcp_server.create_element_template(
        seeded, "Mash Tun", parent_id=bh["id"]
    )
    preview = mcp_server.delete_element_template(bh["id"])
    assert preview["child_count"] == 1
    result = mcp_server.delete_element_template(bh["id"], confirm=True)
    assert "error" in result
