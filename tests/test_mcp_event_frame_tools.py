"""Tests for the event frame tools: operator lifecycle, admin wiring."""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site
from brewerypi.services import (
    create_element,
    create_element_template,
    create_event_frame_attribute_template,
    create_event_frame_template,
    create_lookup,
    create_lookup_value,
)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A New York site: non-exclusive Brewhouse with an exclusive Mash
    Mixer child, Brew/Mashing templates, and a Status attribute."""
    engine = create_engine(f"sqlite:///{tmp_path / 'ef.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        site = Site(
            abbreviation="NY", name="New York",
            enterprise_id=ent.id, timezone="America/New_York",
        )
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        session.add(area)
        session.flush()
        bh_t = create_element_template(
            session, site.id, "Brewhouse", exclusive=False
        )
        mm_t = create_element_template(
            session, site.id, "Mash Mixer", parent_id=bh_t.id
        )
        brew_t = create_event_frame_template(session, bh_t.id, "Brew")
        mash_t = create_event_frame_template(
            session, mm_t.id, "Mashing", parent_id=brew_t.id
        )
        status = create_lookup(session, ent.id, "Status")
        ready = create_lookup_value(session, status.id, "Ready")
        done = create_lookup_value(session, status.id, "Done")
        create_event_frame_attribute_template(
            session, brew_t.id, "Status", lookup_id=status.id,
            default_start_lookup_value_id=ready.id,
            default_end_lookup_value_id=done.id,
        )
        bh = create_element(session, bh_t.id, "BH1", tag_area_id=area.id)
        mm = create_element(
            session, mm_t.id, "MM1",
            tag_area_id=area.id, parent_id=bh.id,
        )
        session.commit()
        ids = {
            "bh": bh.id, "mm": mm.id,
            "brew_t": brew_t.id, "mash_t": mash_t.id,
        }
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def test_create_and_list_in_local_time(seeded):
    frame = mcp_server.create_event_frame(
        seeded["bh"], seeded["brew_t"], "Brew #1",
        started_at="2026-01-15T06:00:00",
    )
    # winter in New York = UTC-5
    assert frame["started_at"] == "2026-01-15T06:00:00-05:00"
    assert frame["timezone"] == "America/New_York"
    assert frame["open"] is True
    rows = mcp_server.list_event_frames(element_id=seeded["bh"])
    assert [r["name"] for r in rows] == ["Brew #1"]


def test_close_writes_end_values_in_local_time(seeded):
    frame = mcp_server.create_event_frame(
        seeded["bh"], seeded["brew_t"], "Brew #1",
        started_at="2026-01-15T06:00:00",
    )
    closed = mcp_server.close_event_frame(
        frame["id"], ended_at="2026-01-15T14:00:00"
    )
    assert closed["ended_at"] == "2026-01-15T14:00:00-05:00"
    assert closed["open"] is False
    # the attribute's boundary values landed on the wired tag
    wiring = mcp_server.list_event_frame_attributes(
        element_id=seeded["bh"]
    )[0]
    readings = mcp_server.get_tag_values(wiring["tag_id"])
    assert readings["count"] == 2


def test_concurrent_brews_allowed_mashing_conflicts(seeded):
    b1 = mcp_server.create_event_frame(
        seeded["bh"], seeded["brew_t"], "Brew #1",
        started_at="2026-01-15T06:00:00",
    )
    mcp_server.create_event_frame(
        seeded["mm"], seeded["mash_t"], "Mashing",
        started_at="2026-01-15T06:00:00",
        ended_at="2026-01-15T07:30:00", parent_id=b1["id"],
    )
    # second brew on the non-exclusive brewhouse: fine
    b2 = mcp_server.create_event_frame(
        seeded["bh"], seeded["brew_t"], "Brew #2",
        started_at="2026-01-15T07:00:00",
    )
    assert "error" not in b2
    # ...but its mashing at 7:00 collides on the exclusive mixer
    clash = mcp_server.create_event_frame(
        seeded["mm"], seeded["mash_t"], "Mashing",
        started_at="2026-01-15T07:00:00", parent_id=b2["id"],
    )
    assert "error" in clash


def test_reopen_and_update(seeded):
    frame = mcp_server.create_event_frame(
        seeded["bh"], seeded["brew_t"], "Brew #1",
        started_at="2026-01-15T06:00:00",
    )
    mcp_server.close_event_frame(
        frame["id"], ended_at="2026-01-15T14:00:00"
    )
    reopened = mcp_server.reopen_event_frame(frame["id"])
    assert reopened["open"] is True
    fixed = mcp_server.update_event_frame(
        frame["id"], ended_at="2026-01-15T15:00:00"
    )
    assert fixed["ended_at"] == "2026-01-15T15:00:00-05:00"


def test_delete_requires_confirm_and_keeps_readings(seeded):
    frame = mcp_server.create_event_frame(
        seeded["bh"], seeded["brew_t"], "Brew #1",
        started_at="2026-01-15T06:00:00",
    )
    wiring = mcp_server.list_event_frame_attributes(
        element_id=seeded["bh"]
    )[0]
    preview = mcp_server.delete_event_frame(frame["id"])
    assert preview.get("confirm_required") is True
    done = mcp_server.delete_event_frame(frame["id"], confirm=True)
    assert done == {"deleted": frame["id"]}
    # the start value written by the frame survives
    assert mcp_server.get_tag_values(wiring["tag_id"])["count"] == 1


def test_open_only_filter(seeded):
    a = mcp_server.create_event_frame(
        seeded["bh"], seeded["brew_t"], "Closed",
        started_at="2026-01-15T06:00:00",
    )
    mcp_server.close_event_frame(a["id"], ended_at="2026-01-15T07:00:00")
    mcp_server.create_event_frame(
        seeded["bh"], seeded["brew_t"], "Running",
        started_at="2026-01-15T08:00:00",
    )
    running = mcp_server.list_event_frames(open_only=True)
    assert [r["name"] for r in running] == ["Running"]


def test_bad_time_returns_error(seeded):
    result = mcp_server.create_event_frame(
        seeded["bh"], seeded["brew_t"], "X", started_at="not-a-time"
    )
    assert "error" in result


def test_lifecycle_on_operator_wiring_writes_on_admin():
    async def names(server):
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    operator = asyncio.run(names(mcp_server.mcp))
    # operators run batches -- the full lifecycle is on their tier
    assert {
        "list_event_frames",
        "get_event_frame",
        "create_event_frame",
        "close_event_frame",
        "reopen_event_frame",
        "update_event_frame",
        "delete_event_frame",
        "list_event_frame_attributes",
        "get_event_frame_attribute",
    } <= operator
    # wiring stays admin
    assert not (
        {"wire_event_frame_attribute", "unwire_event_frame_attribute"}
        & operator
    )
    admin_only = FastMCP("t")
    mcp_server._register_config_tools(admin_only)
    config = asyncio.run(names(admin_only))
    assert {
        "wire_event_frame_attribute",
        "unwire_event_frame_attribute",
    } <= config
