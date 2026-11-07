"""What every web write shares: the one-use confirmation, and reading forms.

A write goes form, then review, then confirm (see ``batch_actions``). The
review page mints a token kept in the session, and confirming spends it,
which stops both a double tap and another site submitting the form. Only
one token is held per sign-in, so a newer review page makes an older one
out of date.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy.orm import Session

from brewerypi import access
from brewerypi.access import OPERATE, READ
from brewerypi.identity import CurrentUser
from brewerypi.services import NotFoundError, ValidationError
from brewerypi.timezones import from_utc, to_utc
from brewerypi.web.rendering import local_time, message_page

_TOKEN_KEY = "write_token"


def mint_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session[_TOKEN_KEY] = token
    return token


def spend_token(request: Request, given: str) -> bool:
    expected = request.session.pop(_TOKEN_KEY, None)
    return bool(expected) and secrets.compare_digest(expected, given)


def stale_page(request: Request, back_to: str, what: str = "batch"):
    return message_page(
        request,
        "That confirmation was already used",
        "This click didn't save anything. You may have pressed the button "
        f"twice, or used an old page, so check the {what} before trying "
        "again.",
        status_code=409,
        link=back_to,
        link_text=f"Check the {what}",
    )


def readable(session: Session, user: CurrentUser, model: type, row_id: int):
    """Return a row the user may read; anything else is simply not found."""
    row = session.get(model, row_id)
    if row is None:
        raise NotFoundError(f"no {model.__name__} with id {row_id}")
    access.check(session, user, row, READ)
    return row


def operable(session: Session, user: CurrentUser, model: type, row_id: int):
    """Return a row the user may change; out of scope is simply not found."""
    row = session.get(model, row_id)
    if row is None:
        raise NotFoundError(f"no {model.__name__} with id {row_id}")
    access.check(session, user, row, OPERATE)
    return row


def parse_id(value: str) -> int | None:
    """Read an optional id from a form; junk is treated as not found."""
    value = value.strip()
    if not value:
        return None
    if not value.isdigit():
        raise NotFoundError(f"no row with id {value!r}")
    return int(value)


def parse_time(value: str, zone: str, which: str) -> datetime:
    """Read a local time from a form into naive UTC."""
    if not value.strip():
        raise ValidationError(f"Enter the {which} date and time.")
    try:
        return to_utc(value.strip(), zone)
    except ValidationError as exc:
        raise ValidationError(
            f"Enter a valid {which} date and time."
        ) from exc


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def input_time(moment: datetime, zone: str) -> str:
    """A UTC moment as a ``datetime-local`` input expects, to the minute."""
    return from_utc(moment, zone)[:16]


def shown_time(moment: datetime, zone: str) -> str:
    return local_time(from_utc(moment, zone))


def future_warning(moment: datetime, what: str) -> list[str]:
    return [f"{what} is in the future."] if moment > utc_now() else []
