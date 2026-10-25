"""UTC <-> local time conversion for the tool boundary.

Readings are stored in UTC (``observed_at`` is naive UTC). The MCP tools
accept and display *local* times; these helpers do the conversion
deterministically and DST-aware via :mod:`zoneinfo`, so the language model
never does timezone math itself.

The zone to use comes from :func:`resolve_timezone`: the signed-in user's
own zone when they have set one (it drives both entry and display), else the
site's. Every caller routes through it, so this is the one place that knows
the order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from brewerypi.identity import current_user
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

    Resolution order: the signed-in user's own zone (for both entry and
    display) -> the site's own timezone -> a system default. A user with no
    zone of their own -- on-site staff, typically -- sees site-local time.
    """
    user = current_user()
    if user is not None and user.timezone:
        if is_valid_timezone(user.timezone):
            return user.timezone
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
