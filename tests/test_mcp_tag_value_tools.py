"""Tests for the operator tag-value tools on the MCP server."""

import asyncio
import datetime

import pytest
from fastmcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Enterprise,
    Lookup,
    LookupValue,
    MeasurementUnit,
    Site,
    Tag,
    TagValue,
)

_TS = datetime.datetime(2026, 6, 1, 8, 0, 0)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Numeric + lookup tags, each with a reading. Yields reading ids."""
    engine = create_engine(f"sqlite:///{tmp_path / 'tv.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        site = Site(abbreviation="S", name="Site", enterprise_id=ent.id)
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        unit = MeasurementUnit(
            enterprise_id=ent.id, abbreviation="°C", name="Celsius"
        )
        lookup = Lookup(enterprise_id=ent.id, name="Stage")
        session.add_all([area, unit, lookup])
        session.flush()
        prim = LookupValue(
            lookup_id=lookup.id, name="Primary", is_selectable=True
        )
        sec = LookupValue(
            lookup_id=lookup.id, name="Secondary", is_selectable=True
        )
        session.add_all([prim, sec])
        num_tag = Tag(
            name="Mash Temp", area_id=area.id, measurement_unit_id=unit.id
        )
        lk_tag = Tag(name="Stage", area_id=area.id, lookup_id=lookup.id)
        session.add_all([num_tag, lk_tag])
        session.flush()
        num_tv = TagValue(tag_id=num_tag.id, timestamp=_TS, value=64.0)
        lk_tv = TagValue(
            tag_id=lk_tag.id, timestamp=_TS, lookup_value_id=prim.id
        )
        session.add_all([num_tv, lk_tv])
        session.commit()
        ids = {"num_tv": num_tv.id, "lk_tv": lk_tv.id, "num_tag": num_tag.id}
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def test_get_tag_values_includes_id(seeded):
    result = mcp_server.get_tag_values(seeded["num_tag"])
    assert result["readings"][0]["id"] == seeded["num_tv"]


def test_get_tag_value(seeded):
    reading = mcp_server.get_tag_value(seeded["num_tv"])
    assert reading["value"] == 64.0
    assert reading["type"] == "numeric"


def test_get_unknown_reading_error(seeded):
    assert "error" in mcp_server.get_tag_value(9999)


def test_update_value(seeded):
    updated = mcp_server.update_tag_value(seeded["num_tv"], value=66.5)
    assert updated["value"] == 66.5


def test_update_timestamp(seeded):
    updated = mcp_server.update_tag_value(
        seeded["num_tv"], timestamp="2026-06-02T09:30:00"
    )
    assert updated["timestamp"] == "2026-06-02T09:30:00"


def test_update_wrong_kind_error(seeded):
    assert "error" in mcp_server.update_tag_value(
        seeded["num_tv"], lookup_value="Primary"
    )


def test_update_lookup_reading(seeded):
    updated = mcp_server.update_tag_value(
        seeded["lk_tv"], lookup_value="Secondary"
    )
    assert updated["value"] == "Secondary"


def test_delete_requires_confirm(seeded):
    preview = mcp_server.delete_tag_value(seeded["num_tv"])
    assert preview.get("confirm_required") is True
    assert "error" not in mcp_server.get_tag_value(seeded["num_tv"])
    done = mcp_server.delete_tag_value(seeded["num_tv"], confirm=True)
    assert done == {"deleted": seeded["num_tv"]}
    assert "error" in mcp_server.get_tag_value(seeded["num_tv"])


def test_tools_on_operator_tier():
    """These corrective tools must be on the base (operator) tier."""

    async def names():
        async with Client(mcp_server.mcp) as client:
            return {t.name for t in await client.list_tools()}

    registered = asyncio.run(names())
    assert {
        "get_tag_value",
        "update_tag_value",
        "delete_tag_value",
    } <= registered
