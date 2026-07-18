"""Tests for the event frame attribute template admin tools."""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Enterprise, Lookup, LookupValue, Site
from brewerypi.services import (
    create_element_template,
    create_event_frame_template,
    create_measurement_unit,
)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A Fermentation event frame template + an FV Status lookup and unit."""
    engine = create_engine(f"sqlite:///{tmp_path / 'efat.db'}")
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
        ferm_t = create_element_template(session, site.id, "Fermenter")
        ef = create_event_frame_template(session, ferm_t.id, "Fermentation")
        status = Lookup(enterprise_id=ent.id, name="FV Status")
        session.add(status)
        session.flush()
        ready = LookupValue(
            lookup_id=status.id, name="Ready to fill", is_selectable=True
        )
        empty = LookupValue(
            lookup_id=status.id, name="Empty", is_selectable=True
        )
        session.add_all([ready, empty])
        unit = create_measurement_unit(session, ent.id, "P", "Plato")
        session.commit()
        ids = {
            "ef": ef.id, "status": status.id,
            "ready": ready.id, "empty": empty.id, "unit": unit.id,
        }
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def test_create_lookup_typed_with_defaults(seeded):
    at = mcp_server.create_event_frame_attribute_template(
        seeded["ef"], "Status", lookup_id=seeded["status"],
        default_start_lookup_value_id=seeded["ready"],
        default_end_lookup_value_id=seeded["empty"],
    )
    assert at["default_start_lookup_value_id"] == seeded["ready"]
    assert at["default_end_lookup_value_id"] == seeded["empty"]


def test_create_numeric_with_defaults(seeded):
    at = mcp_server.create_event_frame_attribute_template(
        seeded["ef"], "Gravity", measurement_unit_id=seeded["unit"],
        default_start_value=12.5, default_end_value=2.5,
    )
    assert at["default_start_value"] == 12.5


def test_create_crossed_defaults_error(seeded):
    result = mcp_server.create_event_frame_attribute_template(
        seeded["ef"], "Gravity", measurement_unit_id=seeded["unit"],
        default_start_lookup_value_id=seeded["ready"],
    )
    assert "error" in result


def test_update_default(seeded):
    at = mcp_server.create_event_frame_attribute_template(
        seeded["ef"], "Gravity", measurement_unit_id=seeded["unit"],
        default_start_value=12.5,
    )
    updated = mcp_server.update_event_frame_attribute_template(
        at["id"], default_start_value=13.0
    )
    assert updated["default_start_value"] == 13.0


def test_delete_requires_confirm(seeded):
    at = mcp_server.create_event_frame_attribute_template(
        seeded["ef"], "Gravity"
    )
    preview = mcp_server.delete_event_frame_attribute_template(at["id"])
    assert preview.get("confirm_required") is True
    done = mcp_server.delete_event_frame_attribute_template(
        at["id"], confirm=True
    )
    assert done == {"deleted": at["id"]}


def test_all_on_admin_tier():
    async def names(server):
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    admin_only = FastMCP("t")
    mcp_server._register_config_tools(admin_only)
    config = asyncio.run(names(admin_only))
    assert {
        "list_event_frame_attribute_templates",
        "create_event_frame_attribute_template",
        "update_event_frame_attribute_template",
        "delete_event_frame_attribute_template",
    } <= config
    # not on the operator (default) tier
    operator = asyncio.run(names(mcp_server.mcp))
    assert not (
        {
            "create_event_frame_attribute_template",
            "delete_event_frame_attribute_template",
        }
        & operator
    )
