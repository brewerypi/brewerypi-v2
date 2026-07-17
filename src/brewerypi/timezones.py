"""UTC <-> local time conversion for the tool boundary.

Readings are stored in UTC (``observed_at`` is naive UTC). The MCP tools
accept and display *local* times; these helpers do the conversion
deterministically and DST-aware via :mod:`zoneinfo`, so the language model
never does timezone math itself.

The zone to use comes from :func:`resolve_timezone`. Today it returns the
site's timezone; once OAuth gives us an authenticated user, that function is
the single place that starts preferring the user's own zone (which, by design,
will drive both entry and display) -- every caller already routes through it,
so no tool changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from brewerypi.models import Site
from brewerypi.services.exceptions import ValidationError

#: Fallback when a site has no usable timezone.
DEFAULT_TIMEZONE = "UTC"


def is_valid_timezone(name: str) -> bool:
    """Return True if ``name`` is an IANA zone that ``zoneinfo`` can load."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def resolve_timezone(session: Session, site: Site) -> str:
    """Return the IANA zone to use for a site's readings.

    Resolution order: (the authenticated user's zone -- not available until
    OAuth; when it is, it will be preferred here for both entry and display)
    -> the site's own timezone -> a system default. This is the one seam the
    OAuth work fills in.
    """
    # TODO(oauth): prefer the authenticated user's timezone here.
    if site.timezone and is_valid_timezone(site.timezone):
        return site.timezone
    return DEFAULT_TIMEZONE


def to_utc(local_iso: str, tz_name: str) -> datetime:
    """Parse a local ISO 8601 time in ``tz_name`` and return naive UTC.

    An offset-aware input is trusted as given. A naive input is interpreted in
    ``tz_name``. DST ambiguity is resolved by ``zoneinfo``'s default
    (``fold=0`` -- the earlier instant for a repeated wall time).
    """
    try:
        dt = datetime.fromisoformat(local_iso)
    except ValueError as exc:
        raise ValidationError(
            f"invalid time {local_iso!r}; use ISO 8601"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def from_utc(dt: datetime, tz_name: str) -> str:
    """Format a stored naive-UTC datetime as local ISO 8601 in ``tz_name``."""
    return (
        dt.replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo(tz_name))
        .isoformat()
    )
