"""Sign-in for the web app: WorkOS AuthKit plus the Brewery Pi allowlist.

The browser is sent to AuthKit, which redirects back with a one-time code;
the server swaps that for the user's identity directly with WorkOS. AuthKit
will authenticate anyone, so authentication grants nothing: the verified email
is matched to ``users`` exactly as on the MCP server (``link_sign_in``).

The signed session cookie holds only the WorkOS subject and session id, never
access. :class:`SignInMiddleware` resolves the subject against ``users`` on
every request and publishes the result through ``signed_in_as``, so removing
someone's access takes effect on their next click and the shared code below
(``access``, ``resolve_timezone``) needs no web-specific path.

Configuration (environment):

    WORKOS_CLIENT_ID    the WorkOS application's client id
    WORKOS_API_KEY      server-side key from the same WorkOS environment
    WEB_BASE_URL        this app's public URL, e.g. https://app.brewerypi.com
    WEB_SESSION_SECRET  signs the session cookie; at least 32 characters
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import anyio.to_thread
import httpx
from sqlalchemy.orm import Session

from brewerypi.identity import CurrentUser, authorize_tier, signed_in_as
from brewerypi.services import (
    PermissionDeniedError,
    get_user_by_subject,
    link_sign_in,
)

WORKOS_API_URL = "https://api.workos.com"

_MIN_SECRET_LENGTH = 32

#: Staff reach the web app with any membership, the operator tier's rule.
_TIER = "operator"


class SignInFailed(Exception):
    """WorkOS could not confirm a sign-in; safe to show the person."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    api_key: str
    base_url: str
    session_secret: str

    @classmethod
    def from_env(cls) -> Settings:
        """Read the settings, refusing to start without a complete set."""
        values = {
            "WORKOS_CLIENT_ID": os.getenv("WORKOS_CLIENT_ID", "").strip(),
            "WORKOS_API_KEY": os.getenv("WORKOS_API_KEY", "").strip(),
            "WEB_BASE_URL": os.getenv("WEB_BASE_URL", "").strip(),
            "WEB_SESSION_SECRET": os.getenv("WEB_SESSION_SECRET", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(
                "web sign-in is not configured: set " + ", ".join(missing)
            )
        if len(values["WEB_SESSION_SECRET"]) < _MIN_SECRET_LENGTH:
            raise RuntimeError(
                "WEB_SESSION_SECRET must be at least "
                f"{_MIN_SECRET_LENGTH} characters"
            )
        return cls(
            client_id=values["WORKOS_CLIENT_ID"],
            api_key=values["WORKOS_API_KEY"],
            base_url=values["WEB_BASE_URL"].rstrip("/"),
            session_secret=values["WEB_SESSION_SECRET"],
        )

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/callback"

    @property
    def is_https(self) -> bool:
        return self.base_url.startswith("https://")


@dataclass(frozen=True)
class SignIn:
    """What WorkOS confirmed about the person who just signed in."""

    subject: str
    email: str | None
    email_verified: bool
    session_id: str | None


#: Swaps AuthKit's one-time code for an identity (replaced in tests).
CodeExchange = Callable[[Settings, str], SignIn]


def authorization_url(settings: Settings, state: str) -> str:
    """Where to send the browser to sign in."""
    query = urlencode(
        {
            "client_id": settings.client_id,
            "provider": "authkit",
            "redirect_uri": settings.redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{WORKOS_API_URL}/user_management/authorize?{query}"


def logout_url(settings: Settings, session_id: str) -> str:
    """Where to send the browser to end its AuthKit session too.

    Clearing only our own cookie would sign a shared tablet's next user
    straight back in as the last one.
    """
    query = urlencode(
        {
            "session_id": session_id,
            "return_to": f"{settings.base_url}/signed-out",
        }
    )
    return f"{WORKOS_API_URL}/user_management/sessions/logout?{query}"


def session_id_from_token(access_token: str | None) -> str | None:
    """Read the session id (``sid``) from a WorkOS access token.

    The signature is not checked because the token came straight from WorkOS
    over TLS, and the id is only ever handed back to WorkOS to sign out.
    """
    try:
        payload = (access_token or "").split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return claims.get("sid")
    except (IndexError, ValueError, AttributeError, binascii.Error):
        return None


def exchange_code(settings: Settings, code: str) -> SignIn:
    """Swap AuthKit's one-time code for the signed-in person's identity."""
    try:
        response = httpx.post(
            f"{WORKOS_API_URL}/user_management/authenticate",
            json={
                "client_id": settings.client_id,
                "client_secret": settings.api_key,
                "grant_type": "authorization_code",
                "code": code,
            },
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise SignInFailed(
            "could not reach WorkOS to confirm your sign-in; try again"
        ) from exc
    if response.status_code != 200:
        raise SignInFailed(
            "WorkOS did not confirm your sign-in "
            f"(HTTP {response.status_code}); try signing in again"
        )
    try:
        data = response.json()
        user = data["user"]
        return SignIn(
            subject=user["id"],
            email=user.get("email"),
            email_verified=bool(user.get("email_verified")),
            session_id=session_id_from_token(data.get("access_token")),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise SignInFailed(
            "WorkOS sent an unexpected answer; try signing in again"
        ) from exc


def complete_sign_in(
    session_factory: Callable[[], Session], sign_in: SignIn
) -> CurrentUser:
    """Match a confirmed sign-in to the allowlist, linking it the first time.

    Raises PermissionDeniedError, saying why, for anyone not allowed in.
    """
    with session_factory() as session:
        user = link_sign_in(
            session, sign_in.subject, sign_in.email, sign_in.email_verified
        )
        # Keep the link even if they have no access yet: it is who they are,
        # and an administrator may well grant it next.
        session.commit()
        authorize_tier(user, _TIER)
        return CurrentUser.from_user(user)


def known_user(
    session_factory: Callable[[], Session], subject: str
) -> CurrentUser | None:
    """Return who a session's subject is now; None if no longer linked.

    Raises PermissionDeniedError if they've been deactivated or have no
    access left.
    """
    with session_factory() as session:
        user = get_user_by_subject(session, subject)
        if user is None:
            return None
        authorize_tier(user, _TIER)
        return CurrentUser.from_user(user)


class SignInMiddleware:
    """Publish who each request is from, checking ``users`` every time.

    It wraps the whole request, unlike a FastAPI dependency, because a sync
    dependency runs in a copy of the context and ``signed_in_as`` wouldn't
    reach the route. Needs ``SessionMiddleware`` outside it. A refusal is
    left in ``scope["state"]["refusal"]`` for the route to show.
    """

    def __init__(
        self, app, session_factory: Callable[[], Session]
    ) -> None:
        self._app = app
        self._session_factory = session_factory

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        session = scope["session"]
        subject = session.get("subject")
        user = None
        if subject:
            try:
                user = await anyio.to_thread.run_sync(
                    known_user, self._session_factory, subject
                )
            except PermissionDeniedError as exc:
                scope.setdefault("state", {})["refusal"] = str(exc)
            else:
                if user is None:
                    session.clear()
        with signed_in_as(user):
            await self._app(scope, receive, send)
