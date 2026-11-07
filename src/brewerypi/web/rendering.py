"""How the web app turns data into pages."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from brewerypi.identity import current_user

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_LOCAL_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})")


def local_time(iso: str | None) -> str:
    """Show a serialized local time as ``20 Oct 2026, 9:45 AM``.

    Serializers have already converted to the right zone, and the wall clock
    is read straight from the text so that nothing here does timezone math.
    """
    match = _LOCAL_TIME.match(iso or "")
    if match is None:
        return iso or ""
    year, month, day, hour, minute = match.groups()
    hour_24 = int(hour)
    period = "AM" if hour_24 < 12 else "PM"
    clock = f"{hour_24 % 12 or 12}:{minute} {period}"
    return f"{int(day)} {_MONTHS[int(month) - 1]} {year}, {clock}"


def elapsed(started_at: datetime, ended_at: datetime) -> str:
    """Say how long a batch ran, in the largest unit that reads naturally."""
    span = ended_at - started_at
    if span < timedelta(0):
        return "0 minutes"
    hours = int(span.total_seconds() // 3600)
    if hours >= 48:
        return f"{hours // 24} days"
    if hours >= 1:
        return f"{hours} hour{'' if hours == 1 else 's'}"
    minutes = int(span.total_seconds() // 60)
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


#: What a write's redirect says happened. Fixed text keyed by the address, so
#: a crafted link can't put words on the page.
_NOTICES = {
    "started": "Batch started.",
    "closed": "Batch closed.",
    "reopened": "Batch reopened.",
    "updated": "Changes saved.",
    "linked": "Transfer saved.",
    "deleted": "Batch deleted.",
    "recorded": "Reading recorded.",
    "reading_updated": "Reading corrected.",
    "reading_deleted": "Reading deleted.",
}

_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
_templates.env.filters["local_time"] = local_time


def page(
    request: Request, template_name: str, status_code: int = 200, **context
) -> HTMLResponse:
    """Render a whole page, never cached: tablets on the floor are shared."""
    return _templates.TemplateResponse(
        request,
        template_name,
        {
            "signed_in_user": current_user(),
            "notice": _NOTICES.get(request.query_params.get("done", "")),
            **context,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def message_page(
    request: Request,
    title: str,
    message: str | None = None,
    status_code: int = 200,
    sign_in: bool = False,
    sign_out: bool = False,
    tone: str | None = None,
    link: str | None = None,
    link_text: str | None = None,
) -> HTMLResponse:
    """Render a page that only says something, with an optional next step."""
    return page(
        request,
        "message.html",
        status_code=status_code,
        title=title,
        message=message,
        sign_in=sign_in,
        sign_out=sign_out,
        tone=tone,
        link=link,
        link_text=link_text,
    )
