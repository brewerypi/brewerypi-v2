"""What every web write shares: the one-use pass, warnings, reading forms.

A write is one form and one press. The form carries a pass minted into the
session, and saving spends it, which stops both a double tap and another
site submitting the form. A press that brings a refusal or a warning back
shows the same form again, with what was typed and a fresh pass; a warning
is accepted by pressing again (see :func:`unaccepted`). Only deletes keep a
separate confirm page.

The last :data:`KEPT_PASSES` passes are kept per sign-in, so several open
pages each work. A pass that is spent or has aged out gets the form back
with :data:`OUT_OF_DATE` and nothing saved.
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

_TOKENS_KEY = "write_tokens"

#: How many unspent passes a sign-in keeps: one per open form, roughly.
KEPT_PASSES = 20

#: What a form says when its pass was spent or aged out.
OUT_OF_DATE = (
    "Nothing was saved: this page was out of date, or the button was "
    "pressed twice. Check the page, then press again."
)


def mint_token(request: Request) -> str:
    token = secrets.token_urlsafe(16)
    request.session.pop("write_token", None)  # the one-pass-only key
    kept = [
        t for t in request.session.get(_TOKENS_KEY, []) if isinstance(t, str)
    ]
    request.session[_TOKENS_KEY] = (kept + [token])[-KEPT_PASSES:]
    return token


def spend_token(request: Request, given: str) -> bool:
    kept = list(request.session.get(_TOKENS_KEY, []))
    for token in kept:
        if given and isinstance(token, str) and secrets.compare_digest(
            token, given
        ):
            kept.remove(token)
            request.session[_TOKENS_KEY] = kept
            return True
    return False


def accepted_key(warnings: list[str]) -> str:
    """What a form sends back to say these exact warnings were seen."""
    return "|".join(warnings)


def unaccepted(warnings: list[str], accepted: str) -> bool:
    """Whether a save must stop to show its warnings.

    Pressing again accepts the warnings the form showed, and only those:
    a value changed into a new warning stops once more.
    """
    return bool(warnings) and accepted != accepted_key(warnings)


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
