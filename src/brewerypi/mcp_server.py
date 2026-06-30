"""Read-only MCP server over the BreweryPi hierarchy and time series.

Exposes a small set of read-only tools so that Claude (via a custom
connector) can browse the enterprise -> site -> area -> tag hierarchy and
query a tag's recorded values. Intended for demos: every tool is a SELECT,
so connected users can read but never modify the shared database.

Run it with the ``brewerypi-mcp`` command (or
``python -m brewerypi.mcp_server``). Configuration is read from the
environment:

    DATABASE_URL   SQLAlchemy URL (default sqlite:///app.db)
    MCP_HOST       bind address (default 127.0.0.1)
    MCP_PORT       bind port (default 8000)
    MCP_PATH       HTTP path for the MCP endpoint (default /mcp)

In deployment the process binds to localhost and a reverse proxy
terminates HTTPS and gates access with a secret path; see the deploy guide.
"""

import os
from datetime import datetime

from fastmcp import FastMCP
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from brewerypi.config import DATABASE_URL
from brewerypi.models import (
    Area,
    Enterprise,
    LookupValue,
    Site,
    Tag,
    TagValue,
)

_engine = create_engine(DATABASE_URL)
_Session = sessionmaker(_engine)

mcp = FastMCP("BreweryPi")


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string into a datetime, or None if not given."""
    return datetime.fromisoformat(value) if value else None


@mcp.tool
def list_enterprises() -> list[dict]:
    """List every enterprise (the top of the BreweryPi hierarchy)."""
    with _Session() as session:
        rows = session.scalars(
            select(Enterprise).order_by(Enterprise.name)
        ).all()
        return [
            {
                "id": e.id,
                "abbreviation": e.abbreviation,
                "name": e.name,
                "description": e.description,
            }
            for e in rows
        ]


@mcp.tool
def list_sites(enterprise_id: int | None = None) -> list[dict]:
    """List sites, optionally filtered to one enterprise by id."""
    with _Session() as session:
        stmt = select(Site).order_by(Site.name)
        if enterprise_id is not None:
            stmt = stmt.where(Site.enterprise_id == enterprise_id)
        return [
            {
                "id": s.id,
                "enterprise_id": s.enterprise_id,
                "abbreviation": s.abbreviation,
                "name": s.name,
                "description": s.description,
            }
            for s in session.scalars(stmt).all()
        ]


@mcp.tool
def list_areas(site_id: int) -> list[dict]:
    """List the areas within a site."""
    with _Session() as session:
        stmt = (
            select(Area).where(Area.site_id == site_id).order_by(Area.name)
        )
        return [
            {
                "id": a.id,
                "site_id": a.site_id,
                "abbreviation": a.abbreviation,
                "name": a.name,
                "description": a.description,
            }
            for a in session.scalars(stmt).all()
        ]


@mcp.tool
def list_tags(area_id: int) -> list[dict]:
    """List the tags in an area.

    Each tag is either numeric (reports a measured value in a unit) or
    lookup-typed (reports a value drawn from a lookup list). ``unit`` is the
    measurement-unit abbreviation for numeric tags; ``lookup_typed`` is true
    when the tag's values come from a lookup.
    """
    with _Session() as session:
        stmt = (
            select(Tag).where(Tag.area_id == area_id).order_by(Tag.name)
        )
        out = []
        for t in session.scalars(stmt).all():
            unit = (
                t.measurement_unit.abbreviation
                if t.measurement_unit is not None
                else None
            )
            out.append(
                {
                    "id": t.id,
                    "area_id": t.area_id,
                    "name": t.name,
                    "description": t.description,
                    "unit": unit,
                    "lookup_typed": t.lookup_id is not None,
                }
            )
        return out


@mcp.tool
def get_tag_values(
    tag_id: int,
    start: str | None = None,
    end: str | None = None,
    limit: int = 200,
) -> dict:
    """Return recorded values for a tag, newest first.

    ``start`` and ``end`` are optional ISO 8601 timestamps that bound the
    time range. ``limit`` caps the number of readings (1-1000). Each reading
    has a ``timestamp`` and a ``value`` that is a number for numeric tags or
    the selected lookup value's name for lookup-typed tags.
    """
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    limit = max(1, min(limit, 1000))
    with _Session() as session:
        tag = session.get(Tag, tag_id)
        if tag is None:
            return {"error": f"no tag with id {tag_id}"}
        stmt = select(TagValue).where(TagValue.tag_id == tag_id)
        if start_dt is not None:
            stmt = stmt.where(TagValue.timestamp >= start_dt)
        if end_dt is not None:
            stmt = stmt.where(TagValue.timestamp <= end_dt)
        stmt = stmt.order_by(TagValue.timestamp.desc()).limit(limit)
        readings = []
        for tv in session.scalars(stmt).all():
            if tv.lookup_value_id is not None:
                lv = session.get(LookupValue, tv.lookup_value_id)
                value: object = lv.name if lv is not None else None
                vtype = "lookup"
            else:
                value = tv.value
                vtype = "numeric"
            readings.append(
                {
                    "timestamp": tv.timestamp.isoformat(),
                    "value": value,
                    "type": vtype,
                }
            )
        return {
            "tag_id": tag_id,
            "tag_name": tag.name,
            "count": len(readings),
            "readings": readings,
        }


@mcp.tool
def tag_value_stats(
    tag_id: int,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """Summary statistics for a numeric tag over an optional time range.

    Returns count, min, max, and average of the numeric readings. Lookup
    readings are ignored. ``start`` and ``end`` are optional ISO 8601
    timestamps.
    """
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    with _Session() as session:
        tag = session.get(Tag, tag_id)
        if tag is None:
            return {"error": f"no tag with id {tag_id}"}
        stmt = select(
            func.count(TagValue.id),
            func.min(TagValue.value),
            func.max(TagValue.value),
            func.avg(TagValue.value),
        ).where(
            TagValue.tag_id == tag_id,
            TagValue.value.is_not(None),
        )
        if start_dt is not None:
            stmt = stmt.where(TagValue.timestamp >= start_dt)
        if end_dt is not None:
            stmt = stmt.where(TagValue.timestamp <= end_dt)
        count, vmin, vmax, vavg = session.execute(stmt).one()
        return {
            "tag_id": tag_id,
            "tag_name": tag.name,
            "count": count,
            "min": vmin,
            "max": vmax,
            "avg": vavg,
        }


@mcp.tool
def browse_hierarchy() -> list[dict]:
    """Return the whole tree: enterprises -> sites -> areas with tag counts.

    A compact overview to orient yourself before drilling in with the other
    tools.
    """
    with _Session() as session:
        out = []
        enterprises = session.scalars(
            select(Enterprise).order_by(Enterprise.name)
        ).all()
        for e in enterprises:
            sites = []
            for s in sorted(e.sites, key=lambda s: s.name):
                areas = [
                    {"id": a.id, "name": a.name, "tag_count": len(a.tags)}
                    for a in sorted(s.areas, key=lambda a: a.name)
                ]
                sites.append({"id": s.id, "name": s.name, "areas": areas})
            out.append({"id": e.id, "name": e.name, "sites": sites})
        return out


def main() -> None:
    """Run the MCP server over streamable HTTP."""
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8000"))
    path = os.getenv("MCP_PATH", "/mcp")
    mcp.run(transport="http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
