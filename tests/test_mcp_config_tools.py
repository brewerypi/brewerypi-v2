"""Tests for the admin-only configuration CRUD tools on the MCP server."""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site, Tag


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Temp DB with one enterprise; point the server's factory at it."""
    engine = create_engine(f"sqlite:///{tmp_path / 'admin.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="BRW", name="Brewery Co")
        session.add(ent)
        session.commit()
        eid = ent.id
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return eid


def test_create_and_list(seeded):
    created = mcp_server.create_measurement_unit(seeded, "°C", "Celsius")
    assert created["name"] == "Celsius"
    assert "id" in created
    listed = mcp_server.list_measurement_units(seeded)
    assert [u["name"] for u in listed] == ["Celsius"]


def test_create_conflict_returns_error(seeded):
    mcp_server.create_measurement_unit(seeded, "°C", "Celsius")
    dup = mcp_server.create_measurement_unit(seeded, "°C", "Centigrade")
    assert "error" in dup


def test_create_unknown_enterprise_returns_error(seeded):
    assert "error" in mcp_server.create_measurement_unit(9999, "°C", "C")


def test_update(seeded):
    unit = mcp_server.create_measurement_unit(seeded, "°C", "Celsius")
    updated = mcp_server.update_measurement_unit(
        unit["id"], name="Degrees Celsius"
    )
    assert updated["name"] == "Degrees Celsius"


def test_delete_requires_confirm(seeded):
    unit = mcp_server.create_measurement_unit(seeded, "°C", "Celsius")
    preview = mcp_server.delete_measurement_unit(unit["id"])
    assert preview.get("confirm_required") is True
    # still present
    assert len(mcp_server.list_measurement_units(seeded)) == 1
    done = mcp_server.delete_measurement_unit(unit["id"], confirm=True)
    assert done == {"deleted": unit["id"]}
    assert mcp_server.list_measurement_units(seeded) == []


def test_delete_refused_when_referenced(seeded):
    unit = mcp_server.create_measurement_unit(seeded, "°C", "Celsius")
    # add a tag referencing the unit
    factory = mcp_server._Session
    with factory() as session:
        site = Site(abbreviation="HQ", name="HQ", enterprise_id=seeded)
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        session.add(area)
        session.flush()
        session.add(
            Tag(name="T", area_id=area.id, measurement_unit_id=unit["id"])
        )
        session.commit()
    result = mcp_server.delete_measurement_unit(unit["id"], confirm=True)
    assert "error" in result


def test_config_tools_registered_by_helper():
    server = FastMCP("t")
    mcp_server._register_config_tools(server)

    async def names():
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    registered = asyncio.run(names())
    expected = {
        "list_measurement_units",
        "create_measurement_unit",
        "update_measurement_unit",
        "delete_measurement_unit",
        "list_lookups",
        "create_lookup",
        "update_lookup",
        "delete_lookup",
        "list_lookup_values",
        "create_lookup_value",
        "update_lookup_value",
        "delete_lookup_value",
        "get_tag",
        "create_tag",
        "update_tag",
        "delete_tag",
        "get_area",
        "create_area",
        "update_area",
        "delete_area",
        "get_site",
        "create_site",
        "update_site",
        "delete_site",
        "get_enterprise",
        "create_enterprise",
        "update_enterprise",
        "delete_enterprise",
    }
    assert expected <= registered
