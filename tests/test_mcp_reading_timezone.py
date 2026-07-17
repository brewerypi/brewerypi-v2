"""Reading tools convert local <-> UTC at the boundary (non-UTC site)."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site, Tag, TagValue


@pytest.fixture
def ny(tmp_path, monkeypatch):
    """A New York site (America/New_York) with one numeric tag."""
    engine = create_engine(f"sqlite:///{tmp_path / 'tz.db'}")
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
        tag = Tag(area_id=area.id, name="FV01.Temperature")
        session.add(tag)
        session.commit()
        ids = {"tag": tag.id}
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return ids, engine


def test_record_interprets_local_and_stores_utc(ny):
    ids, engine = ny
    # 8am local on a winter date (EST = UTC-5) -> stored 13:00 UTC
    out = mcp_server.record_tag_value(
        ids["tag"], value=64.0, observed_at="2026-01-15T08:00:00"
    )
    assert out["timezone"] == "America/New_York"
    assert out["observed_at"] == "2026-01-15T08:00:00-05:00"
    with Session(engine) as session:
        stored = session.scalars(select(TagValue)).one()
        assert stored.observed_at.isoformat() == "2026-01-15T13:00:00"


def test_get_returns_local(ny):
    ids, _ = ny
    mcp_server.record_tag_value(
        ids["tag"], value=64.0, observed_at="2026-07-15T08:00:00"
    )
    got = mcp_server.get_tag_values(ids["tag"])
    assert got["timezone"] == "America/New_York"
    # summer (EDT = UTC-4)
    assert got["readings"][0]["observed_at"] == "2026-07-15T08:00:00-04:00"


def test_range_filter_is_interpreted_local(ny):
    ids, _ = ny
    mcp_server.record_tag_value(
        ids["tag"], value=1.0, observed_at="2026-01-15T08:00:00"
    )
    mcp_server.record_tag_value(
        ids["tag"], value=2.0, observed_at="2026-01-15T10:00:00"
    )
    # a local window that should catch only the 10:00 reading
    got = mcp_server.get_tag_values(
        ids["tag"], start="2026-01-15T09:00:00", end="2026-01-15T11:00:00"
    )
    assert [r["value"] for r in got["readings"]] == [2.0]


def test_stats_range_is_interpreted_local(ny):
    ids, _ = ny
    mcp_server.record_tag_value(
        ids["tag"], value=1.0, observed_at="2026-01-15T08:00:00"
    )
    mcp_server.record_tag_value(
        ids["tag"], value=3.0, observed_at="2026-01-15T10:00:00"
    )
    stats = mcp_server.tag_value_stats(
        ids["tag"], start="2026-01-15T09:00:00"
    )
    assert stats["count"] == 1
    assert stats["min"] == 3.0


def test_record_rejects_bad_time(ny):
    ids, _ = ny
    assert "error" in mcp_server.record_tag_value(
        ids["tag"], value=1.0, observed_at="not-a-time"
    )
