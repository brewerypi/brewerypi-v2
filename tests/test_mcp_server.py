"""Tests for the read-only MCP server tools.

Builds a small temporary database, points the server's session factory at
it, and calls each tool function directly — no network or MCP client is
needed. A temp file (not ``:memory:``) is used so that the tools' own
sessions see the seeded data.

Requires the ``mcp`` extra (``pip install -e ".[dev,mcp]"``) since importing
the server pulls in ``fastmcp``.
"""

import datetime

import pytest
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


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Seed a temp DB and point the server's session factory at it.

    Returns a dict of ids for use in assertions.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        ent = Enterprise(abbreviation="BRW", name="Brewery Co")
        mu = MeasurementUnit(abbreviation="°C", name="Celsius")
        ent.measurement_units.append(mu)
        lookup = Lookup(name="Fermentation Stage")
        lv = LookupValue(name="Primary", is_selectable=True)
        lookup.lookup_values.append(lv)
        ent.lookups.append(lookup)
        site = Site(abbreviation="HQ", name="Headquarters")
        area = Area(abbreviation="BH", name="Brewhouse")
        site.areas.append(area)
        ent.sites.append(site)
        session.add(ent)
        session.flush()

        num_tag = Tag(name="Mash Temp", measurement_unit_id=mu.id)
        lk_tag = Tag(name="Stage", lookup_id=lookup.id)
        area.tags.extend([num_tag, lk_tag])
        session.flush()

        base = datetime.datetime(2026, 6, 1, 8, 0, 0)
        for i, value in enumerate([64.0, 66.0, 68.0]):
            num_tag.tag_values.append(
                TagValue(
                    timestamp=base + datetime.timedelta(minutes=15 * i),
                    value=value,
                )
            )
        lk_tag.tag_values.append(
            TagValue(timestamp=base, lookup_value_id=lv.id)
        )
        session.commit()

        ids = {
            "area_id": area.id,
            "num_tag_id": num_tag.id,
            "lk_tag_id": lk_tag.id,
        }

    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids


def test_list_enterprises(seeded):
    rows = mcp_server.list_enterprises()
    assert [e["name"] for e in rows] == ["Brewery Co"]
    assert rows[0]["abbreviation"] == "BRW"


def test_browse_hierarchy(seeded):
    tree = mcp_server.browse_hierarchy()
    assert tree[0]["name"] == "Brewery Co"
    site = tree[0]["sites"][0]
    assert site["name"] == "Headquarters"
    area = site["areas"][0]
    assert area["name"] == "Brewhouse"
    assert area["tag_count"] == 2


def test_list_tags_reports_unit_and_type(seeded):
    tags = {t["name"]: t for t in mcp_server.list_tags(seeded["area_id"])}
    assert tags["Mash Temp"]["unit"] == "°C"
    assert tags["Mash Temp"]["lookup_typed"] is False
    assert tags["Stage"]["unit"] is None
    assert tags["Stage"]["lookup_typed"] is True


def test_get_tag_values_newest_first(seeded):
    result = mcp_server.get_tag_values(seeded["num_tag_id"])
    assert result["count"] == 3
    assert result["readings"][0]["value"] == 68.0
    assert result["readings"][0]["type"] == "numeric"


def test_get_tag_values_resolves_lookup_value(seeded):
    result = mcp_server.get_tag_values(seeded["lk_tag_id"])
    assert result["count"] == 1
    reading = result["readings"][0]
    assert reading["type"] == "lookup"
    assert reading["value"] == "Primary"


def test_tag_value_stats(seeded):
    stats = mcp_server.tag_value_stats(seeded["num_tag_id"])
    assert stats["count"] == 3
    assert stats["min"] == 64.0
    assert stats["max"] == 68.0
    assert stats["avg"] == pytest.approx(66.0)


def test_unknown_tag_returns_error(seeded):
    assert "error" in mcp_server.get_tag_values(99999)
    assert "error" in mcp_server.tag_value_stats(99999)
