"""Who is making the current call, and whether they may use a tier.

The MCP sign-in middleware resolves the caller to a ``User`` and publishes a
snapshot of them for the duration of one tool call (``signed_in_as``).
Anything below the tool boundary that needs to know who is asking -- today
``resolve_timezone``; later the scoping and attribution work -- reads
``current_user()`` rather than threading a user through every signature.
It is None when nobody is signed in: tests, scripts, and a server run with
``MCP_AUTH=none``.

The snapshot is a plain frozen dataclass, not the ORM row, so it outlives
the session it was loaded in and is safe to read from any thread.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from brewerypi.models import User
from brewerypi.services.exceptions import PermissionDeniedError

#: The two MCP tiers. The tier decides which tools are offered; the user's
#: memberships decide what they may do with them.
TIERS = ("operator", "admin")


@dataclass(frozen=True)
class CurrentUser:
    """The signed-in user, as of the start of this call."""

    id: int
    email: str
    timezone: str | None
    is_superuser: bool

    @classmethod
    def from_user(cls, user: User) -> CurrentUser:
        return cls(
            id=user.id,
            email=user.email,
            timezone=user.timezone,
            is_superuser=user.is_superuser,
        )


_current_user: ContextVar[CurrentUser | None] = ContextVar(
    "brewerypi_current_user", default=None
)


def current_user() -> CurrentUser | None:
    """Return the user making this call, or None if nobody is signed in."""
    return _current_user.get()


@contextmanager
def signed_in_as(user: CurrentUser | None) -> Iterator[None]:
    """Make ``user`` the current user for the duration of the block."""
    token = _current_user.set(user)
    try:
        yield
    finally:
        _current_user.reset(token)


def authorize_tier(user: User, tier: str) -> None:
    """Refuse a user who may not use this tier at all.

    Operator tier: an active superuser, or anyone with a membership. Admin
    tier: an active superuser, or an admin of at least one enterprise.
    Which enterprise a call touches is checked per tool (phase 2); this is
    only the door.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    if not user.is_active:
        raise PermissionDeniedError(
            f"{user.email} has been deactivated in Brewery Pi"
        )
    if user.is_superuser:
        return
    if not user.memberships:
        raise PermissionDeniedError(
            f"{user.email} has not been given access to a brewery yet; ask "
            "an administrator"
        )
    if tier == "admin" and not any(
        m.role == "admin" for m in user.memberships
    ):
        raise PermissionDeniedError(
            f"{user.email} is not an administrator; use the operator "
            "connector"
        )
