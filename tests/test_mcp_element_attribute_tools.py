"""Tests for the element attribute tools: operator reads, admin writes."""

import asyncio
import datetime

import pytest
from fastmcp import Client, FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, MeasurementUnit, Site, TagValue
from brewerypi.services import (
    create_element,
    create_element_attribute_template,
    create_element_template,
    create_tag,
)

_TS = datetime.datetime(2026, 6, 1, 8, 0, 0)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """FV01 under Cellar with a Temperature attribute, already wired."""
    engine = create_engine(f"sqlite:///{tmp_path / 'ea.db'}")
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
            enterprise_id=ent.id, abbreviation="C", name="Celsius"
        )
        session.add_all([area, unit])
        session.flush()
        cellar_t = create_element_template(session, site.id, "Cellar")
        ferm_t = create_element_template(
            session, site.id, "Fermenter", parent_id=cellar_t.id
        )
        create_element_attribute_template(
            session, ferm_t.id, "Temperature",
            measurement_unit_id=unit.id,
        )
        pressure_t = create_element_attribute_template(
            session, ferm_t.id, "Pressure"
        )
        cellar = create_element(
            session, cellar_t.id, "Cellar", tag_area_id=area.id
        )
        fv01 = create_element(
            session, ferm_t.id, "FV01",
            tag_area_id=area.id, parent_id=cellar.id,
        )
        spare = create_tag(session, area.id, "Spare.Existing.Tag")
        session.commit()
        ids = {
            "fv01": fv01.id,
            "area": area.id,
            "pressure_t": pressure_t.id,
            "spare_tag": spare.id,
        }
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def test_list_shows_attribute_and_tag(seeded):
    rows = mcp_server.list_element_attributes(element_id=seeded["fv01"])
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"Temperature", "Pressure"}
    temp = by_name["Temperature"]
    assert temp["tag_name"] == "Cellar.FV01.Temperature"
    assert temp["owns_tag"] is True


def test_get_element_attribute(seeded):
    rows = mcp_server.list_element_attributes(element_id=seeded["fv01"])
    got = mcp_server.get_element_attribute(rows[0]["id"])
    assert got["id"] == rows[0]["id"]


def test_get_unknown_error(seeded):
    assert "error" in mcp_server.get_element_attribute(9999)


def test_unwire_preview_and_confirm(seeded):
    rows = mcp_server.list_element_attributes(element_id=seeded["fv01"])
    target = next(r for r in rows if r["name"] == "Pressure")
    preview = mcp_server.unwire_element_attribute(target["id"])
    assert preview.get("confirm_required") is True
    assert preview["tag_reading_count"] == 0
    done = mcp_server.unwire_element_attribute(
        target["id"], confirm=True
    )
    assert done == {"removed": target["id"]}
    remaining = mcp_server.list_element_attributes(
        element_id=seeded["fv01"]
    )
    assert {r["name"] for r in remaining} == {"Temperature"}


def test_unwire_refused_when_owned_tag_has_readings(seeded):
    rows = mcp_server.list_element_attributes(element_id=seeded["fv01"])
    temp = next(r for r in rows if r["name"] == "Temperature")
    factory = mcp_server._Session
    with factory() as session:
        session.add(
            TagValue(tag_id=temp["tag_id"], observed_at=_TS, value=64.0)
        )
        session.commit()
    result = mcp_server.unwire_element_attribute(
        temp["id"], confirm=True
    )
    assert "error" in result


def test_wire_to_existing_tag_is_adopted(seeded):
    rows = mcp_server.list_element_attributes(element_id=seeded["fv01"])
    pressure = next(r for r in rows if r["name"] == "Pressure")
    mcp_server.unwire_element_attribute(pressure["id"], confirm=True)
    # re-wire it to an existing, unrelated tag
    wired = mcp_server.wire_element_attribute(
        seeded["fv01"], seeded["pressure_t"], tag_id=seeded["spare_tag"]
    )
    assert wired["tag_id"] == seeded["spare_tag"]
    assert wired["owns_tag"] is False


def test_reads_on_operator_writes_on_admin():
    async def names(server):
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    operator = asyncio.run(names(mcp_server.mcp))
    assert {"list_element_attributes", "get_element_attribute"} <= operator
    assert not (
        {"wire_element_attribute", "unwire_element_attribute"} & operator
    )

    admin_only = FastMCP("t")
    mcp_server._register_config_tools(admin_only)
    config = asyncio.run(names(admin_only))
    assert {
        "wire_element_attribute",
        "unwire_element_attribute",
    } <= config
