"""Tests for the transfer and lineage tools on the MCP surface."""

import asyncio

import pytest
from fastmcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site
from brewerypi.services import (
    create_element,
    create_element_template,
    create_event_frame_template,
)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A New York brewhouse and fermenter, with Brew/Knockout/Fermentation."""
    engine = create_engine(f"sqlite:///{tmp_path / 'transfer.db'}")
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
        fv_t = create_element_template(session, site.id, "Fermenter")
        brew_t = create_event_frame_template(session, bh_t.id, "Brew")
        ko_t = create_event_frame_template(session, bh_t.id, "Knockout")
        ferm_t = create_event_frame_template(session, fv_t.id, "Fermentation")
        bh = create_element(session, bh_t.id, "BH1", tag_area_id=area.id)
        fv = create_element(session, fv_t.id, "FV01", tag_area_id=area.id)
        # a wort cooler inside the brewhouse, whose step draws from its brew
        wc_t = create_element_template(
            session, site.id, "Wort Cooler", parent_id=bh_t.id
        )
        wc = create_element(
            session, wc_t.id, "WC1", tag_area_id=area.id, parent_id=bh.id
        )
        cool_t = create_event_frame_template(
            session, wc_t.id, "Wort cooling",
            step_order=1, parent_id=brew_t.id, parent_flow="source",
        )
        session.commit()
        ids = {
            "bh": bh.id, "fv": fv.id, "fv_t": fv_t.id,
            "brew_t": brew_t.id, "ko_t": ko_t.id, "ferm_t": ferm_t.id,
            "wc": wc.id, "wc_t": wc_t.id, "cool_t": cool_t.id,
        }
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def brew_and_ferm(ids):
    brew = mcp_server.create_event_frame(
        ids["bh"], ids["brew_t"], "Brew #1", started_at="2026-07-17T06:00"
    )
    ferm = mcp_server.create_event_frame(
        ids["fv"], ids["ferm_t"], "FV01 B123",
        started_at="2026-07-17T05:00",
    )
    return brew, ferm


def test_knockout_records_both_ends(seeded):
    brew, ferm = brew_and_ferm(seeded)
    ko = mcp_server.create_event_frame(
        seeded["bh"], seeded["ko_t"], "Knockout",
        started_at="2026-07-17T08:00", ended_at="2026-07-17T09:30",
        source_event_frame_id=brew["id"],
        destination_event_frame_id=ferm["id"],
    )
    assert ko["source_event_frame_id"] == brew["id"]
    assert ko["destination_event_frame_id"] == ferm["id"]
    # the payload names the vessel, so a picker list reads without a lookup
    assert ko["element_name"] == "BH1"


def test_destination_is_linked_after_the_transfer_started(seeded):
    brew, ferm = brew_and_ferm(seeded)
    ko = mcp_server.create_event_frame(
        seeded["bh"], seeded["ko_t"], "Knockout",
        started_at="2026-07-17T08:00",
        source_event_frame_id=brew["id"],
    )
    assert ko["destination_event_frame_id"] is None
    linked = mcp_server.link_transfer(
        ko["id"], destination_event_frame_id=ferm["id"]
    )
    assert linked["destination_event_frame_id"] == ferm["id"]
    cleared = mcp_server.link_transfer(ko["id"], clear_destination=True)
    assert cleared["destination_event_frame_id"] is None
    assert cleared["source_event_frame_id"] == brew["id"]


def test_a_transfer_outside_its_source_is_refused(seeded):
    brew, ferm = brew_and_ferm(seeded)
    result = mcp_server.create_event_frame(
        seeded["bh"], seeded["ko_t"], "Knockout",
        started_at="2026-07-17T04:00", ended_at="2026-07-17T05:00",
        source_event_frame_id=brew["id"],
    )
    assert "starts before its source" in result["error"]


def test_closing_the_brew_waits_for_the_knockout(seeded):
    brew, ferm = brew_and_ferm(seeded)
    mcp_server.create_event_frame(
        seeded["bh"], seeded["ko_t"], "Knockout",
        started_at="2026-07-17T08:00",
        source_event_frame_id=brew["id"],
        destination_event_frame_id=ferm["id"],
    )
    blocked = mcp_server.close_event_frame(
        brew["id"], ended_at="2026-07-17T09:30"
    )
    assert "still running out of" in blocked["error"]


def test_picker_lists_open_batches_by_equipment_type(seeded):
    brew_and_ferm(seeded)
    found = mcp_server.list_event_frames(
        element_template_id=seeded["fv_t"], open_only=True
    )
    assert [(f["element_name"], f["name"]) for f in found] == [
        ("FV01", "FV01 B123")
    ]


def test_delete_preview_names_the_transfer_that_blocks_it(seeded):
    brew, ferm = brew_and_ferm(seeded)
    ko = mcp_server.create_event_frame(
        seeded["bh"], seeded["ko_t"], "Knockout",
        started_at="2026-07-17T08:00", ended_at="2026-07-17T09:30",
        source_event_frame_id=brew["id"],
        destination_event_frame_id=ferm["id"],
    )
    preview = mcp_server.delete_event_frame(brew["id"])
    assert preview["confirm_required"] is False
    assert preview["blocking_transfers"] == [
        {"id": ko["id"], "name": "Knockout"}
    ]
    refused = mcp_server.delete_event_frame(brew["id"], confirm=True)
    assert "unlink the transfer first" in refused["error"]
    assert mcp_server.get_event_frame(brew["id"])["name"] == "Brew #1"


def test_lineage_walks_back_in_local_time(seeded):
    brew, ferm = brew_and_ferm(seeded)
    mcp_server.create_event_frame(
        seeded["bh"], seeded["ko_t"], "Knockout",
        started_at="2026-07-17T08:00", ended_at="2026-07-17T09:30",
        source_event_frame_id=brew["id"],
        destination_event_frame_id=ferm["id"],
    )
    result = mcp_server.trace_batch_lineage(ferm["id"], direction="upstream")
    assert {f["name"] for f in result["frames"]} == {
        "FV01 B123", "Knockout", "Brew #1"
    }
    assert result["truncated"] is False
    started = next(
        f["started_at"] for f in result["frames"] if f["name"] == "Brew #1"
    )
    assert started.startswith("2026-07-17T06:00")
    assert all(f["timezone"] == "America/New_York" for f in result["frames"])


def test_lineage_rejects_a_bad_direction(seeded):
    brew, _ = brew_and_ferm(seeded)
    result = mcp_server.trace_batch_lineage(brew["id"], direction="up")
    assert "direction must be" in result["error"]


def test_wort_cooling_names_its_brew_and_closes_with_it(seeded):
    brew, ferm = brew_and_ferm(seeded)
    cooling = mcp_server.create_event_frame(
        seeded["wc"], seeded["cool_t"], "Wort cooling",
        started_at="2026-07-17T08:00", parent_id=brew["id"],
        destination_event_frame_id=ferm["id"],
    )
    # nobody passed a source: the brew it sits inside is the source
    assert cooling["source_event_frame_id"] == brew["id"]
    closed = mcp_server.close_event_frame(
        brew["id"], ended_at="2026-07-17T09:30"
    )
    assert "error" not in closed
    assert mcp_server.get_event_frame(cooling["id"])["open"] is False
    fixed = mcp_server.link_transfer(cooling["id"], clear_source=True)
    assert "cannot be cleared" in fixed["error"]


def test_lineage_leaves_out_a_batch_type_on_request(seeded):
    brew, ferm = brew_and_ferm(seeded)
    mcp_server.create_event_frame(
        seeded["wc"], seeded["cool_t"], "Wort cooling",
        started_at="2026-07-17T08:00", ended_at="2026-07-17T09:00",
        parent_id=brew["id"], destination_event_frame_id=ferm["id"],
    )
    whole = mcp_server.trace_batch_lineage(ferm["id"], direction="upstream")
    assert {f["name"] for f in whole["frames"]} == {
        "FV01 B123", "Wort cooling", "Brew #1"
    }
    assert whole["max_depth"] is None
    skipped = mcp_server.trace_batch_lineage(
        ferm["id"], direction="upstream",
        exclude_event_frame_template_ids=[seeded["cool_t"]],
    )
    assert [f["name"] for f in skipped["frames"]] == ["FV01 B123"]
    assert skipped["truncated"] is False


def test_admin_sets_and_clears_a_batch_types_parent_flow(seeded):
    shown = mcp_server.get_event_frame_template(seeded["cool_t"])
    assert shown["parent_flow"] == "source"
    cleared = mcp_server.update_event_frame_template(
        seeded["cool_t"], clear_parent_flow=True
    )
    assert cleared["parent_flow"] is None
    restored = mcp_server.update_event_frame_template(
        seeded["cool_t"], parent_flow="source"
    )
    assert restored["parent_flow"] == "source"
    refused = mcp_server.update_event_frame_template(
        seeded["brew_t"], parent_flow="source"
    )
    assert "top-level" in refused["error"]


def test_transfer_tools_are_on_the_operator_tier():
    async def names(server):
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    operator = asyncio.run(names(mcp_server.mcp))
    assert {"link_transfer", "trace_batch_lineage"} <= operator
