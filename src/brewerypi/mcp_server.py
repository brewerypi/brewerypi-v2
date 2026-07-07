"""MCP server over the BreweryPi hierarchy and time series.

Exposes tools so that Claude (via a custom connector) can browse the
enterprise -> site -> area -> tag hierarchy, query a tag's recorded values,
and record new readings. All tools are read-only except ``record_tag_value``,
which appends a single reading. There is no per-user auth — the deployment
gates access with one shared secret path — so this is intended for a demo
against a throwaway database that can be rebuilt from the seed.

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
from datetime import datetime, timezone

from fastmcp import FastMCP
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import services
from brewerypi.config import DATABASE_URL
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
from brewerypi.services import ServiceError

_engine = create_engine(DATABASE_URL)
_Session = sessionmaker(_engine)

if _engine.dialect.name == "sqlite":

    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """Enforce foreign keys and wait on locks (writes need both)."""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

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
    has a ``observed_at`` and a ``value`` that is a number for numeric tags or
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
            stmt = stmt.where(TagValue.observed_at >= start_dt)
        if end_dt is not None:
            stmt = stmt.where(TagValue.observed_at <= end_dt)
        stmt = stmt.order_by(TagValue.observed_at.desc()).limit(limit)
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
                    "id": tv.id,
                    "observed_at": tv.observed_at.isoformat(),
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
            stmt = stmt.where(TagValue.observed_at >= start_dt)
        if end_dt is not None:
            stmt = stmt.where(TagValue.observed_at <= end_dt)
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


@mcp.tool
def record_tag_value(
    tag_id: int,
    value: float | None = None,
    lookup_value: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Record a single new reading for a tag (the one write tool).

    For a numeric tag, pass ``value``. For a lookup-typed tag, pass
    ``lookup_value`` as the name of an allowed, selectable lookup value.
    Provide exactly one of the two. ``observed_at`` is an optional ISO 8601
    time and defaults to now. Returns the created reading, or an ``error``
    describing what was wrong (unknown tag, wrong value kind for the tag's
    type, or a lookup value that isn't selectable).
    """
    when = _parse_dt(observed_at) or datetime.now(timezone.utc)
    with _Session() as session:
        tag = session.get(Tag, tag_id)
        if tag is None:
            return {"error": f"no tag with id {tag_id}"}

        if tag.lookup_id is not None:
            if lookup_value is None or value is not None:
                return {
                    "error": (
                        f"tag {tag_id} ({tag.name}) is lookup-typed; pass "
                        "only 'lookup_value'"
                    )
                }
            lv = session.scalars(
                select(LookupValue).where(
                    LookupValue.lookup_id == tag.lookup_id,
                    LookupValue.name == lookup_value,
                    LookupValue.is_selectable.is_(True),
                )
            ).first()
            if lv is None:
                allowed = session.scalars(
                    select(LookupValue.name).where(
                        LookupValue.lookup_id == tag.lookup_id,
                        LookupValue.is_selectable.is_(True),
                    )
                ).all()
                return {
                    "error": (
                        f"'{lookup_value}' is not a selectable value for "
                        f"tag {tag_id}"
                    ),
                    "allowed": list(allowed),
                }
            reading = TagValue(
                tag_id=tag_id, observed_at=when, lookup_value_id=lv.id
            )
            stored: object = lv.name
            vtype = "lookup"
        else:
            if value is None or lookup_value is not None:
                return {
                    "error": (
                        f"tag {tag_id} ({tag.name}) is numeric; pass only "
                        "'value'"
                    )
                }
            reading = TagValue(tag_id=tag_id, observed_at=when, value=value)
            stored = value
            vtype = "numeric"

        session.add(reading)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return {"error": "could not record the reading"}
        session.refresh(reading)
        return {
            "id": reading.id,
            "tag_id": tag_id,
            "tag_name": tag.name,
            "observed_at": reading.observed_at.isoformat(),
            "value": stored,
            "type": vtype,
        }


def _reading_dict(session: Session, tv: TagValue) -> dict:
    if tv.lookup_value_id is not None:
        lv = session.get(LookupValue, tv.lookup_value_id)
        value: object = lv.name if lv is not None else None
        vtype = "lookup"
    else:
        value = tv.value
        vtype = "numeric"
    return {
        "id": tv.id,
        "tag_id": tv.tag_id,
        "observed_at": tv.observed_at.isoformat(),
        "value": value,
        "type": vtype,
    }


@mcp.tool
def get_tag_value(value_id: int) -> dict:
    """Return one recorded reading by id.

    Reading ids come from `get_tag_values`. Use this to check a reading
    before correcting it with `update_tag_value`.
    """
    with _Session() as session:
        try:
            tv = services.get_tag_value(session, value_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        return _reading_dict(session, tv)


@mcp.tool
def update_tag_value(
    value_id: int,
    value: float | None = None,
    lookup_value: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Correct a recorded reading's value and/or observed_at.

    For a numeric tag pass ``value``; for a lookup-typed tag pass
    ``lookup_value`` (the name of a selectable value). ``observed_at`` is an
    optional ISO 8601 time. A reading's type cannot be switched; to move a
    reading to a different tag, delete it and record it again.
    """
    with _Session() as session:
        try:
            tv = services.update_tag_value(
                session, value_id, value, lookup_value, observed_at
            )
            result = _reading_dict(session, tv)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


@mcp.tool
def delete_tag_value(value_id: int, confirm: bool = False) -> dict:
    """Delete a single recorded reading (e.g. one logged to the wrong tag).

    Without ``confirm=true`` this only previews; call again with
    ``confirm=true`` to remove it.
    """
    with _Session() as session:
        try:
            tv = services.get_tag_value(session, value_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            return {
                "confirm_required": True,
                "reading": _reading_dict(session, tv),
                "message": (
                    f"Would delete reading {value_id}. Call again with "
                    "confirm=true."
                ),
            }
        try:
            services.delete_tag_value(session, value_id)
            session.commit()
            return {"deleted": value_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _unit_dict(unit: MeasurementUnit) -> dict:
    return {
        "id": unit.id,
        "enterprise_id": unit.enterprise_id,
        "abbreviation": unit.abbreviation,
        "name": unit.name,
        "description": unit.description,
    }


def list_measurement_units(enterprise_id: int | None = None) -> list[dict]:
    """List measurement units, optionally filtered by enterprise (admin)."""
    with _Session() as session:
        units = services.list_measurement_units(session, enterprise_id)
        return [_unit_dict(u) for u in units]


def create_measurement_unit(
    enterprise_id: int,
    abbreviation: str,
    name: str,
    description: str | None = None,
) -> dict:
    """Create a measurement unit under an enterprise (admin, write).

    ``abbreviation`` and ``name`` must be unique within the enterprise.
    Returns the created unit, or an ``error`` describing the rule it broke.
    """
    with _Session() as session:
        try:
            unit = services.create_measurement_unit(
                session, enterprise_id, abbreviation, name, description
            )
            result = _unit_dict(unit)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_measurement_unit(
    unit_id: int,
    abbreviation: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Update a measurement unit; only provided fields change (admin)."""
    with _Session() as session:
        try:
            unit = services.update_measurement_unit(
                session, unit_id, abbreviation, name, description
            )
            result = _unit_dict(unit)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_measurement_unit(unit_id: int, confirm: bool = False) -> dict:
    """Delete a measurement unit (admin, destructive).

    Without ``confirm=true`` this only previews and does not delete; call
    again with ``confirm=true`` to remove it. Refuses if any tag still
    references the unit.
    """
    with _Session() as session:
        try:
            unit = services.get_measurement_unit(session, unit_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            return {
                "confirm_required": True,
                "unit": _unit_dict(unit),
                "message": (
                    f"Would delete measurement unit {unit_id} "
                    f"({unit.name}). Call again with confirm=true."
                ),
            }
        try:
            services.delete_measurement_unit(session, unit_id)
            session.commit()
            return {"deleted": unit_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _lookup_dict(lookup: Lookup) -> dict:
    return {
        "id": lookup.id,
        "enterprise_id": lookup.enterprise_id,
        "name": lookup.name,
    }


def list_lookups(enterprise_id: int | None = None) -> list[dict]:
    """List lookups, optionally filtered by enterprise (admin)."""
    with _Session() as session:
        rows = services.list_lookups(session, enterprise_id)
        return [_lookup_dict(lk) for lk in rows]


def create_lookup(enterprise_id: int, name: str) -> dict:
    """Create a lookup under an enterprise (admin, write)."""
    with _Session() as session:
        try:
            lookup = services.create_lookup(session, enterprise_id, name)
            result = _lookup_dict(lookup)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_lookup(lookup_id: int, name: str | None = None) -> dict:
    """Rename a lookup (admin, write)."""
    with _Session() as session:
        try:
            lookup = services.update_lookup(session, lookup_id, name)
            result = _lookup_dict(lookup)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_lookup(lookup_id: int, confirm: bool = False) -> dict:
    """Delete a lookup and its values (admin, destructive).

    Without ``confirm=true`` this only previews. Refuses if a tag uses the
    lookup or a recorded reading references one of its values.
    """
    with _Session() as session:
        try:
            lookup = services.get_lookup(session, lookup_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            return {
                "confirm_required": True,
                "lookup": _lookup_dict(lookup),
                "message": (
                    f"Would delete lookup {lookup_id} ({lookup.name}) "
                    "and its values. Call again with confirm=true."
                ),
            }
        try:
            services.delete_lookup(session, lookup_id)
            session.commit()
            return {"deleted": lookup_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _lookup_value_dict(value: LookupValue) -> dict:
    return {
        "id": value.id,
        "lookup_id": value.lookup_id,
        "name": value.name,
        "is_selectable": value.is_selectable,
    }


def list_lookup_values(lookup_id: int) -> list[dict]:
    """List the values belonging to a lookup (admin)."""
    with _Session() as session:
        rows = services.list_lookup_values(session, lookup_id)
        return [_lookup_value_dict(v) for v in rows]


def create_lookup_value(
    lookup_id: int, name: str, is_selectable: bool = True
) -> dict:
    """Create a value under a lookup (admin, write)."""
    with _Session() as session:
        try:
            value = services.create_lookup_value(
                session, lookup_id, name, is_selectable
            )
            result = _lookup_value_dict(value)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_lookup_value(
    value_id: int,
    name: str | None = None,
    is_selectable: bool | None = None,
) -> dict:
    """Update a lookup value; only provided fields change (admin)."""
    with _Session() as session:
        try:
            value = services.update_lookup_value(
                session, value_id, name, is_selectable
            )
            result = _lookup_value_dict(value)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_lookup_value(value_id: int, confirm: bool = False) -> dict:
    """Delete a lookup value (admin, destructive).

    Without ``confirm=true`` this only previews. Refuses if any recorded
    reading references the value.
    """
    with _Session() as session:
        try:
            value = services.get_lookup_value(session, value_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            return {
                "confirm_required": True,
                "lookup_value": _lookup_value_dict(value),
                "message": (
                    f"Would delete lookup value {value_id} "
                    f"({value.name}). Call again with confirm=true."
                ),
            }
        try:
            services.delete_lookup_value(session, value_id)
            session.commit()
            return {"deleted": value_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _tag_dict(tag: Tag) -> dict:
    return {
        "id": tag.id,
        "area_id": tag.area_id,
        "name": tag.name,
        "description": tag.description,
        "lookup_id": tag.lookup_id,
        "measurement_unit_id": tag.measurement_unit_id,
    }


def get_tag(tag_id: int) -> dict:
    """Return one tag's full configuration by id (admin).

    Use the operator `list_tags` to browse tags in an area; this returns the
    raw config fields (lookup_id, measurement_unit_id) needed for editing.
    """
    with _Session() as session:
        try:
            return _tag_dict(services.get_tag(session, tag_id))
        except ServiceError as exc:
            return {"error": str(exc)}


def create_tag(
    area_id: int,
    name: str,
    description: str | None = None,
    lookup_id: int | None = None,
    measurement_unit_id: int | None = None,
) -> dict:
    """Create a tag under an area (admin, write).

    A tag is either lookup-typed (pass ``lookup_id``) or numeric (pass
    ``measurement_unit_id``, or neither) — not both. Any referenced lookup or
    unit must belong to the area's enterprise.
    """
    with _Session() as session:
        try:
            tag = services.create_tag(
                session,
                area_id,
                name,
                description,
                lookup_id,
                measurement_unit_id,
            )
            result = _tag_dict(tag)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_tag(
    tag_id: int,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Update a tag's name and/or description (admin, write).

    Changing a tag's type (lookup vs numeric) is not supported here.
    """
    with _Session() as session:
        try:
            tag = services.update_tag(session, tag_id, name, description)
            result = _tag_dict(tag)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_tag(tag_id: int, confirm: bool = False) -> dict:
    """Delete a tag (admin, destructive).

    Without ``confirm=true`` this only previews. Refuses if the tag has any
    recorded readings, which would otherwise be destroyed with it.
    """
    with _Session() as session:
        try:
            tag = services.get_tag(session, tag_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            return {
                "confirm_required": True,
                "tag": _tag_dict(tag),
                "message": (
                    f"Would delete tag {tag_id} ({tag.name}). Call again "
                    "with confirm=true (refused if it has readings)."
                ),
            }
        try:
            services.delete_tag(session, tag_id)
            session.commit()
            return {"deleted": tag_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _area_dict(area: Area) -> dict:
    return {
        "id": area.id,
        "site_id": area.site_id,
        "abbreviation": area.abbreviation,
        "name": area.name,
        "description": area.description,
    }


def get_area(area_id: int) -> dict:
    """Return one area's full configuration by id (admin).

    Use the operator `list_areas` to browse areas within a site.
    """
    with _Session() as session:
        try:
            return _area_dict(services.get_area(session, area_id))
        except ServiceError as exc:
            return {"error": str(exc)}


def create_area(
    site_id: int,
    abbreviation: str,
    name: str,
    description: str | None = None,
) -> dict:
    """Create an area under a site (admin, write)."""
    with _Session() as session:
        try:
            area = services.create_area(
                session, site_id, abbreviation, name, description
            )
            result = _area_dict(area)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_area(
    area_id: int,
    abbreviation: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Update an area; only provided fields change (admin, write)."""
    with _Session() as session:
        try:
            area = services.update_area(
                session, area_id, abbreviation, name, description
            )
            result = _area_dict(area)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_area(area_id: int, confirm: bool = False) -> dict:
    """Delete an area and its tags (admin, destructive).

    Without ``confirm=true`` this previews and reports how many tags would be
    removed. Refuses if any recorded reading exists under the area's tags.
    """
    with _Session() as session:
        try:
            area = services.get_area(session, area_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            tag_count = session.scalar(
                select(func.count())
                .select_from(Tag)
                .where(Tag.area_id == area_id)
            )
            return {
                "confirm_required": True,
                "area": _area_dict(area),
                "tag_count": tag_count,
                "message": (
                    f"Would delete area {area_id} ({area.name}) and its "
                    f"{tag_count} tag(s). Refused if any readings exist. "
                    "Call again with confirm=true."
                ),
            }
        try:
            services.delete_area(session, area_id)
            session.commit()
            return {"deleted": area_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _register_config_tools(server: FastMCP) -> None:
    """Register the admin-only configuration CRUD tools on a server."""
    for tool in (
        list_measurement_units,
        create_measurement_unit,
        update_measurement_unit,
        delete_measurement_unit,
        list_lookups,
        create_lookup,
        update_lookup,
        delete_lookup,
        list_lookup_values,
        create_lookup_value,
        update_lookup_value,
        delete_lookup_value,
        get_tag,
        create_tag,
        update_tag,
        delete_tag,
        get_area,
        create_area,
        update_area,
        delete_area,
    ):
        server.tool(tool)


# Config editing is gated by role. The default tier is "operator" (read +
# record_tag_value); the admin tier (MCP_ROLE=admin) additionally exposes the
# config CRUD tools. Run the admin tier on its own port and secret path.
if os.getenv("MCP_ROLE", "operator") == "admin":
    _register_config_tools(mcp)


def main() -> None:
    """Run the MCP server over streamable HTTP."""
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8000"))
    path = os.getenv("MCP_PATH", "/mcp")
    mcp.run(transport="http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
