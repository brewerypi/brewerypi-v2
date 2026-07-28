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
    Element,
    ElementAttribute,
    ElementAttributeTemplate,
    ElementTemplate,
    Enterprise,
    EventFrame,
    EventFrameAttribute,
    EventFrameAttributeTemplate,
    EventFrameTemplate,
    Lookup,
    LookupValue,
    MeasurementUnit,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services import ServiceError
from brewerypi.timezones import (
    DEFAULT_TIMEZONE,
    from_utc,
    resolve_timezone,
    to_utc,
)

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

_SHARED_INSTRUCTIONS = """
Brewery Pi is a process-data historian for breweries. You are talking to
brewery staff -- a brewmaster, cellar operator, QA technician, packaging
operator, or COO -- not to database users. Speak their language.

WHAT THE PIECES ARE CALLED
- enterprise = the company. site = a location, usually called by its place
  name ("Atlanta") or house name ("Brew 1"). area = a physical zone within a
  site (brewhouse, cellar, packaging) or a virtual grouping (utilities).
- element = a piece of equipment that carries more than one measurement: a
  fermenter, kettle, mash mixer, centrifuge, canning line.
- tag = one measurement or data point. element attribute = a measurement
  belonging to a piece of equipment.
- event frame = a batch: a window of time on one piece of equipment. Call it
  what the equipment makes -- a brew (brewhouse), a fermentation (FV), a
  centrifugation, bright beer or conditioning (BBT), a canning / bottling /
  kegging run (packaging line).
- lookup = a list of text options (e.g. an "FV Status" list). Its lookup
  values are the options on that list.
- measurement unit = unit of measurement.

THE THREE SHAPES OF DATA (the distinction that actually matters)
- A tag on its own holds continuous data.
- An element with element attributes holds grouped continuous data for one
  piece of equipment.
- An event frame with event frame attributes holds batch data.

HOW TO TALK
- Lead with brewery words, not schema words. Say "equipment", "batch",
  "measurement", "list". Do not open with "element", "event frame", "tag" or
  "attribute" -- but if the user says them first, use them back. Some users
  know these systems well and dislike being talked around.
- Follow the brewery's units. Gravity may be Plato or specific gravity;
  temperature may be F or C. Report whatever the tag carries and never
  silently convert.
- Vessel status, brand names and similar text values live in lists. Read the
  list rather than assuming its options -- every brewery names them
  differently.
- Be specific about batch type. "A packaging run" is ambiguous; a canning run
  and a kegging run measure different things. Ask which.
- One plain, direct voice for everyone. Let the question set the depth: brief
  for data entry, fuller for analysis.
- Answer first, then add anything notable in a sentence. Do not invent
  significance the data does not support.
- Report what the data says with confidence. Be careful about prescribing
  process changes -- target ranges per brand are not in the system yet, so
  defer to the brewer on whether a number is acceptable.
- Ask when a request is ambiguous (twenty fermenters, one "the fermenter"),
  and confirm before writing anything.
"""

_OPERATOR_INSTRUCTIONS = _SHARED_INSTRUCTIONS + """
YOUR ROLE HERE
You can read the whole brewery model, record and correct readings, and run
batches -- start a brew or fermentation, close it, reopen one closed by
mistake, and fix its times. You cannot change how the brewery is set up; if
someone needs new equipment or a new measurement defined, point them at an
administrator.

To find data: browse to the equipment, list its attributes to get the tag
behind each measurement, then read or record values on that tag. To see what
happened during a batch, get the batch's start and end and read the tag over
that window.
"""

_ADMIN_INSTRUCTIONS = _SHARED_INSTRUCTIONS + """
YOUR ROLE HERE
You have full configuration access as well as everything an operator can do.
You can set a brewery up from nothing.

A SENSIBLE SETUP ORDER
1. Create the company, then the site (set its timezone -- readings are
   entered and displayed in it).
2. Create the areas: brewhouse, cellar, packaging, utilities.
3. Create the units of measurement and any lists (e.g. FV Status with its
   options) the brewery needs.
4. Create element templates -- one per KIND of equipment (Fermenter, Kettle,
   Mash Mixer), not one per vessel. Mark a template non-exclusive only if
   several batches can run on one of its vessels at once (a brewhouse can;
   a mash mixer cannot).
5. Add element attribute templates for the measurements each kind of
   equipment carries (temperature, pressure, status).
6. Add event frame templates for the batches each kind of equipment runs
   (Brew on the brewhouse, Fermentation on the fermenter), with their
   attribute templates and default start/end values.
7. Create the elements themselves -- FV01 through FV12 -- giving each a tag
   area. Tags and wiring are created automatically at this point, so define
   the templates before the equipment.

Work in bulk when the user describes their brewery in bulk ("twelve
fermenters") -- create them all rather than asking one at a time. Confirm the
shape back to them briefly when you are done with a step.
"""

mcp = FastMCP(
    "BreweryPi",
    instructions=(
        _ADMIN_INSTRUCTIONS
        if os.getenv("MCP_ROLE", "operator") == "admin"
        else _OPERATOR_INSTRUCTIONS
    ),
)


def _zone_for_tag(session: Session, tag: Tag) -> str:
    """Resolve the IANA timezone for a tag's readings (via its site)."""
    area = session.get(Area, tag.area_id)
    if area is None:
        return DEFAULT_TIMEZONE
    site = session.get(Site, area.site_id)
    if site is None:
        return DEFAULT_TIMEZONE
    return resolve_timezone(session, site)


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

    ``start`` and ``end`` are optional ISO 8601 times in the site's local
    timezone that bound the range. ``limit`` caps the number of readings
    (1-1000). Each reading has an ``observed_at`` (local time) and a ``value``
    that is a number for numeric tags or the selected lookup value's name for
    lookup-typed tags.
    """
    limit = max(1, min(limit, 1000))
    with _Session() as session:
        tag = session.get(Tag, tag_id)
        if tag is None:
            return {"error": f"no tag with id {tag_id}"}
        zone = _zone_for_tag(session, tag)
        try:
            start_dt = to_utc(start, zone) if start is not None else None
            end_dt = to_utc(end, zone) if end is not None else None
        except ServiceError as exc:
            return {"error": str(exc)}
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
                    "observed_at": from_utc(tv.observed_at, zone),
                    "value": value,
                    "type": vtype,
                }
            )
        return {
            "tag_id": tag_id,
            "tag_name": tag.name,
            "timezone": zone,
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
    readings are ignored. ``start`` and ``end`` are optional ISO 8601 times in
    the site's local timezone.
    """
    with _Session() as session:
        tag = session.get(Tag, tag_id)
        if tag is None:
            return {"error": f"no tag with id {tag_id}"}
        zone = _zone_for_tag(session, tag)
        try:
            start_dt = to_utc(start, zone) if start is not None else None
            end_dt = to_utc(end, zone) if end is not None else None
        except ServiceError as exc:
            return {"error": str(exc)}
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
    """Record a reading for a measurement.

    For a numeric measurement pass ``value``; for a text one pass
    ``lookup_value``, naming an option on its list. Exactly one of the two.
    ``observed_at`` is an optional ISO 8601 time in the brewery's local time
    and defaults to now. Confirm what you are about to record before writing.
    Returns the reading, or an ``error`` explaining what was wrong.
    """
    with _Session() as session:
        tag = session.get(Tag, tag_id)
        if tag is None:
            return {"error": f"no tag with id {tag_id}"}
        zone = _zone_for_tag(session, tag)
        try:
            when = (
                to_utc(observed_at, zone)
                if observed_at is not None
                else datetime.now(timezone.utc).replace(tzinfo=None)
            )
        except ServiceError as exc:
            return {"error": str(exc)}

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
            "observed_at": from_utc(reading.observed_at, zone),
            "timezone": zone,
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
    tag = session.get(Tag, tv.tag_id)
    zone = _zone_for_tag(session, tag) if tag is not None else DEFAULT_TIMEZONE
    return {
        "id": tv.id,
        "tag_id": tv.tag_id,
        "observed_at": from_utc(tv.observed_at, zone),
        "timezone": zone,
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
    optional ISO 8601 time in the site's local timezone. A reading's type
    cannot be switched; to move a reading to a different tag, delete it and
    record it again.
    """
    with _Session() as session:
        try:
            if observed_at is not None:
                existing = services.get_tag_value(session, value_id)
                tag = session.get(Tag, existing.tag_id)
                zone = (
                    _zone_for_tag(session, tag)
                    if tag is not None
                    else DEFAULT_TIMEZONE
                )
                observed_at = to_utc(observed_at, zone).isoformat()
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


def _element_dict(e: Element) -> dict:
    return {
        "id": e.id,
        "element_template_id": e.element_template_id,
        "tag_area_id": e.tag_area_id,
        "parent_id": e.parent_id,
        "name": e.name,
        "description": e.description,
    }


@mcp.tool
def list_elements(
    element_template_id: int | None = None,
    site_id: int | None = None,
    parent_id: int | None = None,
) -> list[dict]:
    """List equipment, optionally filtered.

    Filter by ``element_template_id`` for every vessel of one kind (all the
    fermenters), by ``site_id`` for a whole brewery, or by ``parent_id`` for
    what sits inside a given piece of equipment. Each row includes
    ``parent_id`` so you can rebuild the layout.
    """
    with _Session() as session:
        rows = services.list_elements(
            session, element_template_id, site_id, parent_id
        )
        return [_element_dict(e) for e in rows]


@mcp.tool
def get_element(element_id: int) -> dict:
    """Return one element by id."""
    with _Session() as session:
        try:
            return _element_dict(services.get_element(session, element_id))
        except ServiceError as exc:
            return {"error": str(exc)}


def _element_attribute_dict(
    session: Session, ea: ElementAttribute
) -> dict:
    """Attribute rows carry the template's name and the wired tag's name.

    An element attribute has no name of its own -- it inherits the attribute
    template's -- so both are resolved here for a usable view.
    """
    template = session.get(
        ElementAttributeTemplate, ea.element_attribute_template_id
    )
    tag = session.get(Tag, ea.tag_id)
    return {
        "id": ea.id,
        "element_id": ea.element_id,
        "element_attribute_template_id": ea.element_attribute_template_id,
        "name": template.name if template is not None else None,
        "tag_id": ea.tag_id,
        "tag_name": tag.name if tag is not None else None,
        "owns_tag": ea.owns_tag,
    }


@mcp.tool
def list_element_attributes(
    element_id: int | None = None,
    element_attribute_template_id: int | None = None,
) -> list[dict]:
    """List the measurements a piece of equipment carries.

    Filter by ``element_id`` to see one vessel's measurements -- FV01's
    temperature, pressure, status. Each row gives the measurement name and the
    ``tag_id``/``tag_name`` holding its data, which you then read with
    `get_tag_values` or write with `record_tag_value`. This is the usual way
    in: equipment -> measurement -> readings.
    """
    with _Session() as session:
        rows = services.list_element_attributes(
            session, element_id, element_attribute_template_id
        )
        return [_element_attribute_dict(session, ea) for ea in rows]


@mcp.tool
def get_element_attribute(element_attribute_id: int) -> dict:
    """Return one element attribute (with its name and wired tag)."""
    with _Session() as session:
        try:
            ea = services.get_element_attribute(
                session, element_attribute_id
            )
        except ServiceError as exc:
            return {"error": str(exc)}
        return _element_attribute_dict(session, ea)


def _event_frame_template_dict(t: EventFrameTemplate) -> dict:
    return {
        "id": t.id,
        "element_template_id": t.element_template_id,
        "parent_id": t.parent_id,
        "name": t.name,
        "description": t.description,
    }


@mcp.tool
def list_event_frame_templates(
    element_template_id: int | None = None,
    parent_id: int | None = None,
) -> list[dict]:
    """List event frame templates (batch-window types), optionally filtered.

    These are the batch types an operator can start on an element (e.g. a
    "Brew" on a Brewhouse, a "Fermentation" on a Fermenter). Filter by
    ``element_template_id`` or by ``parent_id`` for a nested template's
    children.
    """
    with _Session() as session:
        rows = services.list_event_frame_templates(
            session, element_template_id, parent_id
        )
        return [_event_frame_template_dict(t) for t in rows]


@mcp.tool
def get_event_frame_template(template_id: int) -> dict:
    """Return one event frame template by id."""
    with _Session() as session:
        try:
            return _event_frame_template_dict(
                services.get_event_frame_template(session, template_id)
            )
        except ServiceError as exc:
            return {"error": str(exc)}


def _zone_for_element(session: Session, element: Element) -> str:
    """Resolve the IANA timezone for an element (via its template's site)."""
    template = session.get(ElementTemplate, element.element_template_id)
    if template is None:
        return DEFAULT_TIMEZONE
    site = session.get(Site, template.site_id)
    if site is None:
        return DEFAULT_TIMEZONE
    return resolve_timezone(session, site)


def _zone_for_frame(session: Session, frame: EventFrame) -> str:
    element = session.get(Element, frame.element_id)
    if element is None:
        return DEFAULT_TIMEZONE
    return _zone_for_element(session, element)


def _event_frame_dict(session: Session, f: EventFrame) -> dict:
    zone = _zone_for_frame(session, f)
    return {
        "id": f.id,
        "element_id": f.element_id,
        "event_frame_template_id": f.event_frame_template_id,
        "parent_id": f.parent_id,
        "name": f.name,
        "started_at": from_utc(f.started_at, zone),
        "ended_at": (
            from_utc(f.ended_at, zone) if f.ended_at is not None else None
        ),
        "open": f.ended_at is None,
        "timezone": zone,
    }


@mcp.tool
def list_event_frames(
    element_id: int | None = None,
    event_frame_template_id: int | None = None,
    parent_id: int | None = None,
    open_only: bool = False,
) -> list[dict]:
    """List batches, newest first, optionally filtered.

    A batch is a named window of time on one piece of equipment -- a brew, a
    fermentation, a canning run. ``open_only=true`` returns just what is still
    running, which answers "what is going on right now". Times are in the
    brewery's local time. Take a batch's start and end and pass them to
    `get_tag_values` to see what a measurement did during it.
    """
    with _Session() as session:
        rows = services.list_event_frames(
            session,
            element_id,
            event_frame_template_id,
            parent_id,
            open_only,
        )
        return [_event_frame_dict(session, f) for f in rows]


@mcp.tool
def get_event_frame(event_frame_id: int) -> dict:
    """Return one event frame (batch) by id."""
    with _Session() as session:
        try:
            frame = services.get_event_frame(session, event_frame_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        return _event_frame_dict(session, frame)


@mcp.tool
def create_event_frame(
    element_id: int,
    event_frame_template_id: int,
    name: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    parent_id: int | None = None,
) -> dict:
    """Start a batch on a piece of equipment.

    Call the batch what the equipment makes: a brew on the brewhouse, a
    fermentation on a fermenter, a canning or kegging run on a packaging line
    (ask which -- they measure different things). Use the batch type the
    brewery defined for that equipment.

    ``started_at``/``ended_at`` are ISO 8601 times in the brewery's local
    time; ``started_at`` defaults to now, and leaving ``ended_at`` unset means
    the batch is still running. A batch inside another (a mashing within a
    brew) needs ``parent_id``. Any default start values are recorded
    automatically. Refused if the equipment can only run one batch at a time
    and is already busy.
    """
    with _Session() as session:
        element = session.get(Element, element_id)
        if element is None:
            return {"error": f"no element with id {element_id}"}
        zone = _zone_for_element(session, element)
        try:
            start_dt = (
                to_utc(started_at, zone) if started_at is not None else None
            )
            end_dt = to_utc(ended_at, zone) if ended_at is not None else None
            frame = services.create_event_frame(
                session,
                element_id,
                event_frame_template_id,
                name,
                start_dt,
                end_dt,
                parent_id,
            )
            result = _event_frame_dict(session, frame)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


@mcp.tool
def close_event_frame(
    event_frame_id: int, ended_at: str | None = None
) -> dict:
    """Close (end) a running batch.

    ``ended_at`` is an ISO 8601 local time, defaulting to now. Records any
    default end values at that time -- setting a vessel back to "Empty", for
    instance -- and closes any nested batches still running inside it.
    """
    with _Session() as session:
        try:
            frame = services.get_event_frame(session, event_frame_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        zone = _zone_for_frame(session, frame)
        try:
            end_dt = to_utc(ended_at, zone) if ended_at is not None else None
            frame = services.close_event_frame(
                session, event_frame_id, end_dt
            )
            result = _event_frame_dict(session, frame)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


@mcp.tool
def reopen_event_frame(event_frame_id: int) -> dict:
    """Reopen a closed event frame, making it running again.

    Use this when a batch was closed by mistake. If instead the end time was
    simply wrong, prefer `update_event_frame` with the corrected ``ended_at``.
    Values written at the old end time stay put -- correct them yourself if
    needed. Refused if reopening would overlap another batch on
    single-occupancy equipment.
    """
    with _Session() as session:
        try:
            frame = services.reopen_event_frame(session, event_frame_id)
            result = _event_frame_dict(session, frame)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


@mcp.tool
def update_event_frame(
    event_frame_id: int,
    name: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> dict:
    """Rename an event frame or correct its start/end times.

    Times are ISO 8601 in the site's local timezone. Moving a boundary
    re-checks overlap and that nested batches still fit; readings already
    recorded are left where they are.
    """
    with _Session() as session:
        try:
            frame = services.get_event_frame(session, event_frame_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        zone = _zone_for_frame(session, frame)
        kwargs: dict = {}
        try:
            if started_at is not None:
                kwargs["started_at"] = to_utc(started_at, zone)
            if ended_at is not None:
                kwargs["ended_at"] = to_utc(ended_at, zone)
            frame = services.update_event_frame(
                session, event_frame_id, name, **kwargs
            )
            result = _event_frame_dict(session, frame)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


@mcp.tool
def delete_event_frame(
    event_frame_id: int, confirm: bool = False
) -> dict:
    """Delete an event frame and any nested batches inside it (destructive).

    Recorded readings and tags are NOT deleted -- a batch is only a window over
    them. Without ``confirm=true`` this previews.
    """
    with _Session() as session:
        try:
            frame = services.get_event_frame(session, event_frame_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            child_count = session.scalar(
                select(func.count())
                .select_from(EventFrame)
                .where(EventFrame.parent_id == event_frame_id)
            )
            return {
                "confirm_required": True,
                "event_frame": _event_frame_dict(session, frame),
                "nested_count": child_count,
                "message": (
                    f"Would delete event frame {event_frame_id} "
                    f"({frame.name}) and {child_count} nested batch(es). "
                    "Readings and tags are kept. Call again with "
                    "confirm=true."
                ),
            }
        try:
            services.delete_event_frame(session, event_frame_id)
            session.commit()
            return {"deleted": event_frame_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _event_frame_attribute_dict(
    session: Session, a: EventFrameAttribute
) -> dict:
    template = session.get(
        EventFrameAttributeTemplate,
        a.event_frame_attribute_template_id,
    )
    tag = session.get(Tag, a.tag_id)
    return {
        "id": a.id,
        "element_id": a.element_id,
        "event_frame_attribute_template_id": (
            a.event_frame_attribute_template_id
        ),
        "name": template.name if template is not None else None,
        "tag_id": a.tag_id,
        "tag_name": tag.name if tag is not None else None,
        "owns_tag": a.owns_tag,
    }


@mcp.tool
def list_event_frame_attributes(
    element_id: int | None = None,
    event_frame_attribute_template_id: int | None = None,
) -> list[dict]:
    """List the tags a piece of equipment's batches write through.

    Wiring is per equipment (not per batch): every batch on this element
    records its attribute values on these tags. Each row gives the attribute
    name and the ``tag_id``/``tag_name`` holding the data.
    """
    with _Session() as session:
        rows = services.list_event_frame_attributes(
            session, element_id, event_frame_attribute_template_id
        )
        return [_event_frame_attribute_dict(session, a) for a in rows]


@mcp.tool
def get_event_frame_attribute(event_frame_attribute_id: int) -> dict:
    """Return one event frame attribute wiring (with its name and tag)."""
    with _Session() as session:
        try:
            a = services.get_event_frame_attribute(
                session, event_frame_attribute_id
            )
        except ServiceError as exc:
            return {"error": str(exc)}
        return _event_frame_attribute_dict(session, a)


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
    """Create a list of text options (admin, write).

    Use a list wherever a measurement is text rather than a number -- vessel
    status, brand. Add its options with `create_lookup_value`.
    """
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
    """List the options on a list.

    Read these rather than assuming -- every brewery names its vessel statuses
    and brands differently.
    """
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
    """Delete a tag and all of its recorded readings (admin, destructive).

    Readings cannot outlive their tag, so they go with it. Without
    ``confirm=true`` this previews how many readings would be destroyed and
    over what period. Refused while an element or event frame attribute is
    still wired to the tag.
    """
    with _Session() as session:
        try:
            tag = services.get_tag(session, tag_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            count, first, last = session.execute(
                select(
                    func.count(TagValue.id),
                    func.min(TagValue.observed_at),
                    func.max(TagValue.observed_at),
                ).where(TagValue.tag_id == tag_id)
            ).one()
            zone = _zone_for_tag(session, tag)
            span = (
                f" recorded between {from_utc(first, zone)} and "
                f"{from_utc(last, zone)}"
                if count
                else ""
            )
            return {
                "confirm_required": True,
                "tag": _tag_dict(tag),
                "reading_count": count,
                "first_reading": (
                    from_utc(first, zone) if count else None
                ),
                "last_reading": from_utc(last, zone) if count else None,
                "timezone": zone,
                "message": (
                    f"Would delete tag {tag_id} ({tag.name}) and its "
                    f"{count} reading(s){span}. This cannot be undone. "
                    "Call again with confirm=true."
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
    """Create an area within a brewery (admin, write).

    A physical zone -- brewhouse, cellar, packaging -- or a virtual grouping
    such as utilities for boilers, glycol and compressed air. Areas hold the
    tags for the equipment in them.
    """
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


def _site_dict(site: Site) -> dict:
    return {
        "id": site.id,
        "enterprise_id": site.enterprise_id,
        "abbreviation": site.abbreviation,
        "name": site.name,
        "description": site.description,
        "timezone": site.timezone,
    }


def get_site(site_id: int) -> dict:
    """Return one site's full configuration by id (admin).

    Use the operator `list_sites` to browse sites within an enterprise.
    """
    with _Session() as session:
        try:
            return _site_dict(services.get_site(session, site_id))
        except ServiceError as exc:
            return {"error": str(exc)}


def create_site(
    enterprise_id: int,
    abbreviation: str,
    name: str,
    description: str | None = None,
    timezone: str = "UTC",
) -> dict:
    """Create a brewery location (admin, write).

    Usually named for the place ("Atlanta") or the house name ("Brew 1").
    ``timezone`` is an IANA name (e.g. "America/New_York"); every reading and
    batch time at this site is entered and displayed in it, so set it now.
    Defaults to "UTC".
    """
    with _Session() as session:
        try:
            site = services.create_site(
                session,
                enterprise_id,
                abbreviation,
                name,
                description,
                timezone,
            )
            result = _site_dict(site)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_site(
    site_id: int,
    abbreviation: str | None = None,
    name: str | None = None,
    description: str | None = None,
    timezone: str | None = None,
) -> dict:
    """Update a site; only provided fields change (admin, write).

    ``timezone`` is an IANA name (e.g. "America/New_York").
    """
    with _Session() as session:
        try:
            site = services.update_site(
                session,
                site_id,
                abbreviation,
                name,
                description,
                timezone,
            )
            result = _site_dict(site)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_site(site_id: int, confirm: bool = False) -> dict:
    """Delete a site and its areas/tags (admin, destructive).

    Without ``confirm=true`` this previews and reports how many areas and tags
    would be removed. Refuses if any recorded reading exists under the site.
    """
    with _Session() as session:
        try:
            site = services.get_site(session, site_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            area_count = session.scalar(
                select(func.count())
                .select_from(Area)
                .where(Area.site_id == site_id)
            )
            tag_count = session.scalar(
                select(func.count())
                .select_from(Tag)
                .join(Area, Tag.area_id == Area.id)
                .where(Area.site_id == site_id)
            )
            return {
                "confirm_required": True,
                "site": _site_dict(site),
                "area_count": area_count,
                "tag_count": tag_count,
                "message": (
                    f"Would delete site {site_id} ({site.name}) with its "
                    f"{area_count} area(s) and {tag_count} tag(s). Refused "
                    "if any readings exist. Call again with confirm=true."
                ),
            }
        try:
            services.delete_site(session, site_id)
            session.commit()
            return {"deleted": site_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _enterprise_dict(ent: Enterprise) -> dict:
    return {
        "id": ent.id,
        "abbreviation": ent.abbreviation,
        "name": ent.name,
        "description": ent.description,
    }


def get_enterprise(enterprise_id: int) -> dict:
    """Return one enterprise's full configuration by id (admin).

    Use the operator `list_enterprises` to browse all enterprises.
    """
    with _Session() as session:
        try:
            return _enterprise_dict(
                services.get_enterprise(session, enterprise_id)
            )
        except ServiceError as exc:
            return {"error": str(exc)}


def create_enterprise(
    abbreviation: str,
    name: str,
    description: str | None = None,
) -> dict:
    """Create an enterprise (admin, write).

    ``abbreviation`` and ``name`` are globally unique.
    """
    with _Session() as session:
        try:
            ent = services.create_enterprise(
                session, abbreviation, name, description
            )
            result = _enterprise_dict(ent)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_enterprise(
    enterprise_id: int,
    abbreviation: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Update an enterprise; only provided fields change (admin, write)."""
    with _Session() as session:
        try:
            ent = services.update_enterprise(
                session, enterprise_id, abbreviation, name, description
            )
            result = _enterprise_dict(ent)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_enterprise(enterprise_id: int, confirm: bool = False) -> dict:
    """Delete an enterprise and its entire subtree (admin, destructive).

    Without ``confirm=true`` this previews and reports the full blast radius
    (sites, areas, tags, lookups, measurement units). Refuses if any recorded
    reading exists under the enterprise, or if any of its lookup values are
    referenced by a reading.
    """
    with _Session() as session:
        try:
            ent = services.get_enterprise(session, enterprise_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            sites = session.scalar(
                select(func.count())
                .select_from(Site)
                .where(Site.enterprise_id == enterprise_id)
            )
            areas = session.scalar(
                select(func.count())
                .select_from(Area)
                .join(Site, Area.site_id == Site.id)
                .where(Site.enterprise_id == enterprise_id)
            )
            tags = session.scalar(
                select(func.count())
                .select_from(Tag)
                .join(Area, Tag.area_id == Area.id)
                .join(Site, Area.site_id == Site.id)
                .where(Site.enterprise_id == enterprise_id)
            )
            lookups = session.scalar(
                select(func.count())
                .select_from(Lookup)
                .where(Lookup.enterprise_id == enterprise_id)
            )
            units = session.scalar(
                select(func.count())
                .select_from(MeasurementUnit)
                .where(MeasurementUnit.enterprise_id == enterprise_id)
            )
            return {
                "confirm_required": True,
                "enterprise": _enterprise_dict(ent),
                "site_count": sites,
                "area_count": areas,
                "tag_count": tags,
                "lookup_count": lookups,
                "measurement_unit_count": units,
                "message": (
                    f"Would delete enterprise {enterprise_id} "
                    f"({ent.name}) and its entire subtree: {sites} site(s), "
                    f"{areas} area(s), {tags} tag(s), {lookups} lookup(s), "
                    f"{units} measurement unit(s). Refused if any readings "
                    "exist. Call again with confirm=true."
                ),
            }
        try:
            services.delete_enterprise(session, enterprise_id)
            session.commit()
            return {"deleted": enterprise_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _element_template_dict(t: ElementTemplate) -> dict:
    return {
        "id": t.id,
        "site_id": t.site_id,
        "parent_id": t.parent_id,
        "name": t.name,
        "description": t.description,
        "exclusive": t.exclusive,
    }


def list_element_templates(site_id: int | None = None) -> list[dict]:
    """List element templates, optionally filtered by site (admin).

    Each row includes ``parent_id`` (null for a top-level template), so the
    site's template tree can be reconstructed from the flat list.
    """
    with _Session() as session:
        rows = services.list_element_templates(session, site_id)
        return [_element_template_dict(t) for t in rows]


def create_element_template(
    site_id: int,
    name: str,
    description: str | None = None,
    parent_id: int | None = None,
    exclusive: bool = True,
) -> dict:
    """Define a KIND of equipment (admin, write).

    One template per kind -- "Fermenter", "Kettle", "Mash Mixer" -- not one
    per vessel; the individual vessels are created later as elements. Pass
    ``parent_id`` to nest a kind inside another (a Mash Mixer within a
    Brewhouse). Name is unique within the site.

    ``exclusive`` (default true) means only one batch at a time can run on a
    vessel of this kind -- right for a mash mixer or fermenter. Set it false
    for umbrella equipment such as a brewhouse, where several brews can be in
    progress at once.
    """
    with _Session() as session:
        try:
            t = services.create_element_template(
                session, site_id, name, description, parent_id, exclusive
            )
            result = _element_template_dict(t)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_element_template(
    template_id: int,
    name: str | None = None,
    description: str | None = None,
    parent_id: int | None = None,
    make_top_level: bool = False,
    exclusive: bool | None = None,
) -> dict:
    """Update an element template (admin, write).

    Re-parenting: set ``parent_id`` to move the template under another
    template (same site, no cycles), or set ``make_top_level=true`` to
    promote it to a top-level template. Leave both unset to keep the current
    parent. ``name``/``description``/``exclusive`` change only when provided.
    """
    with _Session() as session:
        try:
            if make_top_level:
                t = services.update_element_template(
                    session,
                    template_id,
                    name,
                    description,
                    parent_id=None,
                    exclusive=exclusive,
                )
            elif parent_id is not None:
                t = services.update_element_template(
                    session,
                    template_id,
                    name,
                    description,
                    parent_id=parent_id,
                    exclusive=exclusive,
                )
            else:
                t = services.update_element_template(
                    session,
                    template_id,
                    name,
                    description,
                    exclusive=exclusive,
                )
            result = _element_template_dict(t)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_element_template(
    template_id: int, confirm: bool = False
) -> dict:
    """Delete an element template (admin, destructive).

    Without ``confirm=true`` this previews and reports the child count.
    Refuses if the template has child templates (delete or reparent those
    first).
    """
    with _Session() as session:
        try:
            t = services.get_element_template(session, template_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            child_count = session.scalar(
                select(func.count())
                .select_from(ElementTemplate)
                .where(ElementTemplate.parent_id == template_id)
            )
            return {
                "confirm_required": True,
                "element_template": _element_template_dict(t),
                "child_count": child_count,
                "message": (
                    f"Would delete element template {template_id} "
                    f"({t.name}). Refused if it has children "
                    f"({child_count}). Call again with confirm=true."
                ),
            }
        try:
            services.delete_element_template(session, template_id)
            session.commit()
            return {"deleted": template_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def create_element(
    element_template_id: int,
    name: str,
    description: str | None = None,
    tag_area_id: int | None = None,
    parent_id: int | None = None,
) -> dict:
    """Create one actual piece of equipment (admin, write).

    This is a specific vessel or machine -- FV01, Kettle 2 -- instancing a
    kind defined by ``element_template_id``. Give it ``tag_area_id`` (an area
    in the same site) and its measurements are wired up automatically, so
    define the kind's attributes before creating the equipment. Pass
    ``parent_id`` when this sits inside another piece of equipment (a mash
    mixer within a brewhouse).
    """
    with _Session() as session:
        try:
            el = services.create_element(
                session,
                element_template_id,
                name,
                description,
                tag_area_id,
                parent_id,
            )
            result = _element_dict(el)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_element(
    element_id: int,
    name: str | None = None,
    description: str | None = None,
    tag_area_id: int | None = None,
    clear_tag_area: bool = False,
    parent_id: int | None = None,
) -> dict:
    """Update an element (admin, write).

    ``name``/``description`` change when provided. Set ``tag_area_id`` to
    assign a tag area (same site), or ``clear_tag_area=true`` to unassign it.
    Set ``parent_id`` to move the element under another valid parent (one
    instancing the template's parent template). The element's template can't
    be changed.
    """
    kwargs: dict = {}
    if clear_tag_area:
        kwargs["tag_area_id"] = None
    elif tag_area_id is not None:
        kwargs["tag_area_id"] = tag_area_id
    if parent_id is not None:
        kwargs["parent_id"] = parent_id
    with _Session() as session:
        try:
            el = services.update_element(
                session, element_id, name, description, **kwargs
            )
            result = _element_dict(el)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_element(element_id: int, confirm: bool = False) -> dict:
    """Delete an element (admin, destructive).

    Without ``confirm=true`` this previews and reports the child count.
    Refuses if the element has child elements.
    """
    with _Session() as session:
        try:
            el = services.get_element(session, element_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            child_count = session.scalar(
                select(func.count())
                .select_from(Element)
                .where(Element.parent_id == element_id)
            )
            return {
                "confirm_required": True,
                "element": _element_dict(el),
                "child_count": child_count,
                "message": (
                    f"Would delete element {element_id} ({el.name}). "
                    f"Refused if it has children ({child_count}). Call "
                    "again with confirm=true."
                ),
            }
        try:
            services.delete_element(session, element_id)
            session.commit()
            return {"deleted": element_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _attribute_template_dict(t: ElementAttributeTemplate) -> dict:
    return {
        "id": t.id,
        "element_template_id": t.element_template_id,
        "lookup_id": t.lookup_id,
        "measurement_unit_id": t.measurement_unit_id,
        "name": t.name,
        "description": t.description,
    }


def list_element_attribute_templates(
    element_template_id: int | None = None,
) -> list[dict]:
    """List element attribute templates, optionally filtered by template.

    Each attribute template defines an attribute (name + optional lookup or
    measurement unit) on an element template.
    """
    with _Session() as session:
        rows = services.list_element_attribute_templates(
            session, element_template_id
        )
        return [_attribute_template_dict(t) for t in rows]


def create_element_attribute_template(
    element_template_id: int,
    name: str,
    description: str | None = None,
    lookup_id: int | None = None,
    measurement_unit_id: int | None = None,
) -> dict:
    """Define a measurement that a kind of equipment carries (admin, write).

    For example temperature or pressure on a Fermenter. Numeric measurements
    take ``measurement_unit_id``; text measurements (a vessel status, a brand)
    take ``lookup_id`` naming the list of options -- one or the other, never
    both. Every vessel of this kind gets the measurement automatically, and
    existing vessels are updated too.
    """
    with _Session() as session:
        try:
            t = services.create_element_attribute_template(
                session,
                element_template_id,
                name,
                description,
                lookup_id,
                measurement_unit_id,
            )
            result = _attribute_template_dict(t)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_element_attribute_template(
    attribute_template_id: int,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Update an attribute template's name and/or description (admin, write).

    Changing its type (lookup vs numeric) is not supported here.
    """
    with _Session() as session:
        try:
            t = services.update_element_attribute_template(
                session, attribute_template_id, name, description
            )
            result = _attribute_template_dict(t)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_element_attribute_template(
    attribute_template_id: int, confirm: bool = False
) -> dict:
    """Delete an attribute template (admin, destructive).

    Without ``confirm=true`` this only previews.
    """
    with _Session() as session:
        try:
            t = services.get_element_attribute_template(
                session, attribute_template_id
            )
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            return {
                "confirm_required": True,
                "element_attribute_template": _attribute_template_dict(t),
                "message": (
                    f"Would delete attribute template "
                    f"{attribute_template_id} ({t.name}). Call again with "
                    "confirm=true."
                ),
            }
        try:
            services.delete_element_attribute_template(
                session, attribute_template_id
            )
            session.commit()
            return {"deleted": attribute_template_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def wire_element_attribute(
    element_id: int,
    element_attribute_template_id: int,
    tag_id: int | None = None,
) -> dict:
    """Wire an attribute template onto an element (admin, write).

    Attributes are normally wired automatically when an element is created (or
    gains a tag area, or when a new attribute template is added), so this is
    for the manual case -- in particular, passing ``tag_id`` links an existing
    tag instead of auto-creating one. Without ``tag_id``, the tag is found or
    created by generated name (e.g. ``Cellar.FV01.Temperature``).
    """
    with _Session() as session:
        try:
            element = services.get_element(session, element_id)
            template = services.get_element_attribute_template(
                session, element_attribute_template_id
            )
            ea = services.wire_element_attribute(
                session, element, template, tag_id
            )
            result = _element_attribute_dict(session, ea)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def unwire_element_attribute(
    element_attribute_id: int, confirm: bool = False
) -> dict:
    """Remove an element attribute (admin, destructive).

    An auto-created tag is deleted with it only when it is disposable (no
    readings, nothing else wired to it); a tag carrying history or an adopted
    tag is left standing. Without ``confirm=true`` this only previews.
    """
    with _Session() as session:
        try:
            ea = services.get_element_attribute(
                session, element_attribute_id
            )
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            readings = session.scalar(
                select(func.count())
                .select_from(TagValue)
                .where(TagValue.tag_id == ea.tag_id)
            )
            if not ea.owns_tag:
                fate = "its tag was adopted and will be left in place"
            elif readings:
                fate = (
                    f"its tag has {readings} reading(s) and will be left "
                    "standing"
                )
            else:
                fate = "its tag would be deleted too (if unused elsewhere)"
            return {
                "confirm_required": True,
                "element_attribute": _element_attribute_dict(session, ea),
                "tag_reading_count": readings,
                "message": (
                    f"Would remove element attribute "
                    f"{element_attribute_id}; {fate}. Call again with "
                    "confirm=true."
                ),
            }
        try:
            services.unwire_element_attribute(
                session, element_attribute_id
            )
            session.commit()
            return {"removed": element_attribute_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def create_event_frame_template(
    element_template_id: int,
    name: str,
    description: str | None = None,
    parent_id: int | None = None,
) -> dict:
    """Define a KIND of batch that a kind of equipment runs (admin, write).

    A "Brew" on the brewhouse, a "Fermentation" on the fermenter, a "Canning
    Run" on the canning line. Pass ``parent_id`` to nest a batch inside
    another -- a Mashing within a Brew -- which requires this batch's
    equipment to sit directly inside the parent batch's equipment.
    """
    with _Session() as session:
        try:
            t = services.create_event_frame_template(
                session, element_template_id, name, description, parent_id
            )
            result = _event_frame_template_dict(t)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_event_frame_template(
    template_id: int,
    name: str | None = None,
    description: str | None = None,
    parent_id: int | None = None,
    make_top_level: bool = False,
) -> dict:
    """Update an event frame template (admin, write).

    Re-parent by setting ``parent_id`` (A1 mirror still applies) or
    ``make_top_level=true``; leave both unset to keep the current parent.
    ``name``/``description`` change only when provided.
    """
    with _Session() as session:
        try:
            if make_top_level:
                t = services.update_event_frame_template(
                    session, template_id, name, description, parent_id=None
                )
            elif parent_id is not None:
                t = services.update_event_frame_template(
                    session,
                    template_id,
                    name,
                    description,
                    parent_id=parent_id,
                )
            else:
                t = services.update_event_frame_template(
                    session, template_id, name, description
                )
            result = _event_frame_template_dict(t)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_event_frame_template(
    template_id: int, confirm: bool = False
) -> dict:
    """Delete an event frame template (admin, destructive).

    Without ``confirm=true`` this previews and reports the child count.
    Refuses if the template has child templates.
    """
    with _Session() as session:
        try:
            t = services.get_event_frame_template(session, template_id)
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            child_count = session.scalar(
                select(func.count())
                .select_from(EventFrameTemplate)
                .where(EventFrameTemplate.parent_id == template_id)
            )
            return {
                "confirm_required": True,
                "event_frame_template": _event_frame_template_dict(t),
                "child_count": child_count,
                "message": (
                    f"Would delete event frame template {template_id} "
                    f"({t.name}). Refused if it has children "
                    f"({child_count}). Call again with confirm=true."
                ),
            }
        try:
            services.delete_event_frame_template(session, template_id)
            session.commit()
            return {"deleted": template_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def _event_frame_attribute_template_dict(
    t: EventFrameAttributeTemplate,
) -> dict:
    return {
        "id": t.id,
        "event_frame_template_id": t.event_frame_template_id,
        "lookup_id": t.lookup_id,
        "measurement_unit_id": t.measurement_unit_id,
        "name": t.name,
        "description": t.description,
        "default_start_value": t.default_start_value,
        "default_end_value": t.default_end_value,
        "default_start_lookup_value_id": t.default_start_lookup_value_id,
        "default_end_lookup_value_id": t.default_end_lookup_value_id,
    }


def list_event_frame_attribute_templates(
    event_frame_template_id: int | None = None,
) -> list[dict]:
    """List event frame attribute templates (admin, read).

    Each defines an attribute (name + optional lookup or unit) on an event
    frame template, with default start/end values applied at the frame's
    boundaries.
    """
    with _Session() as session:
        rows = services.list_event_frame_attribute_templates(
            session, event_frame_template_id
        )
        return [
            _event_frame_attribute_template_dict(t) for t in rows
        ]


def create_event_frame_attribute_template(
    event_frame_template_id: int,
    name: str,
    description: str | None = None,
    lookup_id: int | None = None,
    measurement_unit_id: int | None = None,
    default_start_value: float | None = None,
    default_end_value: float | None = None,
    default_start_lookup_value_id: int | None = None,
    default_end_lookup_value_id: int | None = None,
) -> dict:
    """Define a measurement recorded on a kind of batch (admin, write).

    Numeric measurements take ``measurement_unit_id``; text ones take
    ``lookup_id``. The defaults are recorded automatically when a batch starts
    and ends -- so a Fermentation's status can be set to "Ready to fill" on
    start and "Empty" on close. Numeric defaults use
    ``default_start_value``/``default_end_value``; text defaults use
    ``default_start_lookup_value_id``/``default_end_lookup_value_id``, which
    must be selectable options on the attribute's list.
    """
    with _Session() as session:
        try:
            t = services.create_event_frame_attribute_template(
                session,
                event_frame_template_id,
                name,
                description,
                lookup_id,
                measurement_unit_id,
                default_start_value,
                default_end_value,
                default_start_lookup_value_id,
                default_end_lookup_value_id,
            )
            result = _event_frame_attribute_template_dict(t)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def update_event_frame_attribute_template(
    attribute_template_id: int,
    name: str | None = None,
    description: str | None = None,
    default_start_value: float | None = None,
    default_end_value: float | None = None,
    default_start_lookup_value_id: int | None = None,
    default_end_lookup_value_id: int | None = None,
) -> dict:
    """Update an event frame attribute template (admin, write).

    Edits name/description and/or the default values (a provided default is
    set, re-validated against the attribute's type). Changing the type is not
    supported here.
    """
    kwargs: dict = {}
    if default_start_value is not None:
        kwargs["default_start_value"] = default_start_value
    if default_end_value is not None:
        kwargs["default_end_value"] = default_end_value
    if default_start_lookup_value_id is not None:
        kwargs["default_start_lookup_value_id"] = (
            default_start_lookup_value_id
        )
    if default_end_lookup_value_id is not None:
        kwargs["default_end_lookup_value_id"] = default_end_lookup_value_id
    with _Session() as session:
        try:
            t = services.update_event_frame_attribute_template(
                session, attribute_template_id, name, description, **kwargs
            )
            result = _event_frame_attribute_template_dict(t)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def delete_event_frame_attribute_template(
    attribute_template_id: int, confirm: bool = False
) -> dict:
    """Delete an event frame attribute template (admin, destructive).

    Without ``confirm=true`` this only previews.
    """
    with _Session() as session:
        try:
            t = services.get_event_frame_attribute_template(
                session, attribute_template_id
            )
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            return {
                "confirm_required": True,
                "event_frame_attribute_template": (
                    _event_frame_attribute_template_dict(t)
                ),
                "message": (
                    f"Would delete event frame attribute template "
                    f"{attribute_template_id} ({t.name}). Call again with "
                    "confirm=true."
                ),
            }
        try:
            services.delete_event_frame_attribute_template(
                session, attribute_template_id
            )
            session.commit()
            return {"deleted": attribute_template_id}
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def wire_event_frame_attribute(
    element_id: int,
    event_frame_attribute_template_id: int,
    tag_id: int | None = None,
) -> dict:
    """Wire an event frame attribute template onto an element (admin, write).

    Normally automatic (on element creation, tag-area assignment, or when a
    new attribute template is added), so this is for the manual case --
    notably passing ``tag_id`` to link an existing tag instead of
    auto-creating one.
    """
    with _Session() as session:
        try:
            element = services.get_element(session, element_id)
            template = services.get_event_frame_attribute_template(
                session, event_frame_attribute_template_id
            )
            a = services.wire_event_frame_attribute(
                session, element, template, tag_id
            )
            result = _event_frame_attribute_dict(session, a)
            session.commit()
            return result
        except ServiceError as exc:
            session.rollback()
            return {"error": str(exc)}


def unwire_event_frame_attribute(
    event_frame_attribute_id: int, confirm: bool = False
) -> dict:
    """Remove an event frame attribute wiring (admin, destructive).

    An auto-created tag is deleted with it only when it is disposable (no
    readings, nothing else wired to it); otherwise it is left standing.
    """
    with _Session() as session:
        try:
            a = services.get_event_frame_attribute(
                session, event_frame_attribute_id
            )
        except ServiceError as exc:
            return {"error": str(exc)}
        if not confirm:
            readings = session.scalar(
                select(func.count())
                .select_from(TagValue)
                .where(TagValue.tag_id == a.tag_id)
            )
            if not a.owns_tag:
                fate = "its tag was adopted and will be left in place"
            elif readings:
                fate = (
                    f"its tag has {readings} reading(s) and will be left "
                    "standing"
                )
            else:
                fate = "its tag would be deleted too (if unused elsewhere)"
            return {
                "confirm_required": True,
                "event_frame_attribute": _event_frame_attribute_dict(
                    session, a
                ),
                "tag_reading_count": readings,
                "message": (
                    f"Would remove event frame attribute "
                    f"{event_frame_attribute_id}; {fate}. Call again with "
                    "confirm=true."
                ),
            }
        try:
            services.unwire_event_frame_attribute(
                session, event_frame_attribute_id
            )
            session.commit()
            return {"removed": event_frame_attribute_id}
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
        get_site,
        create_site,
        update_site,
        delete_site,
        get_enterprise,
        create_enterprise,
        update_enterprise,
        delete_enterprise,
        list_element_templates,
        create_element_template,
        update_element_template,
        delete_element_template,
        create_element,
        update_element,
        delete_element,
        list_element_attribute_templates,
        create_element_attribute_template,
        update_element_attribute_template,
        delete_element_attribute_template,
        wire_element_attribute,
        unwire_element_attribute,
        create_event_frame_template,
        update_event_frame_template,
        delete_event_frame_template,
        list_event_frame_attribute_templates,
        create_event_frame_attribute_template,
        update_event_frame_attribute_template,
        delete_event_frame_attribute_template,
        wire_event_frame_attribute,
        unwire_event_frame_attribute,
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
