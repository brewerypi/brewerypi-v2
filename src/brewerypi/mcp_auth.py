"""Sign-in for the MCP server: WorkOS AuthKit plus the Brewery Pi allowlist.

AuthKit runs the OAuth flow (including the dynamic client registration that
Claude's connectors use) and issues JWTs; FastMCP's ``AuthKitProvider``
verifies them. Verification only proves *someone* signed in -- AuthKit will
authenticate anyone -- so :class:`SignInMiddleware` then resolves the token
to a row in ``users`` and refuses every tool call from a person who is not
on the list, is deactivated, or may not use this tier.

The token carries WorkOS's user id (``sub``) but not an email, so a subject
seen for the first time is looked up through the WorkOS API to get its
verified email, matched to the allowlist, and linked; later calls match on
the subject alone.

Configuration (environment):

    MCP_AUTH         "authkit" (default) or "none" -- local development only
    AUTHKIT_DOMAIN   e.g. https://your-app.authkit.app
    MCP_BASE_URL     this tier's public URL, e.g. https://mcp.brewerypi.com
    WORKOS_API_KEY   server-side key (sk_...) for the email lookup
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

import anyio.to_thread
import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.workos import AuthKitProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from sqlalchemy.orm import Session

from brewerypi.identity import (
    CurrentUser,
    authorize_tier,
    signed_in_as,
)
from brewerypi.services import (
    PermissionDeniedError,
    get_user_by_subject,
    link_sign_in,
)

WORKOS_API_URL = "https://api.workos.com"

_AUTHKIT_SETTINGS = ("AUTHKIT_DOMAIN", "MCP_BASE_URL", "WORKOS_API_KEY")


@dataclass(frozen=True)
class SignInIdentity:
    """What the identity provider says about a subject."""

    email: str | None
    email_verified: bool


#: Looks up a subject's identity at the provider (async: it is HTTP).
IdentityLookup = Callable[[str], Awaitable[SignInIdentity]]


def auth_mode() -> str:
    """Return the configured sign-in mode, refusing anything unknown."""
    mode = os.getenv("MCP_AUTH", "authkit").strip().lower()
    if mode not in ("authkit", "none"):
        raise RuntimeError(
            f"MCP_AUTH must be 'authkit' or 'none', not {mode!r}"
        )
    return mode


def build_auth_provider() -> AuthKitProvider:
    """Build the AuthKit token verifier from the environment.

    Raises when a setting is missing, so a misconfigured server refuses to
    start rather than starting open.
    """
    missing = [name for name in _AUTHKIT_SETTINGS if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "sign-in is not configured: set "
            + ", ".join(missing)
            + " (or MCP_AUTH=none for local development only)"
        )
    return AuthKitProvider(
        authkit_domain=_with_scheme(os.environ["AUTHKIT_DOMAIN"]),
        base_url=os.environ["MCP_BASE_URL"],
    )


def _with_scheme(domain: str) -> str:
    """Accept an AuthKit domain with or without ``https://``.

    The dashboard shows it bare (``your-app.authkit.app``), and FastMCP
    refuses anything but a full URL -- with an error that names a pydantic
    validator, not the setting.
    """
    domain = domain.strip().rstrip("/")
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    return domain


async def fetch_workos_identity(subject: str) -> SignInIdentity:
    """Ask the WorkOS User Management API for a user's email."""
    url = f"{WORKOS_API_URL}/user_management/users/{quote(subject, safe='')}"
    headers = {"Authorization": f"Bearer {os.environ['WORKOS_API_KEY']}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ToolError(
            "could not reach WorkOS to confirm your sign-in; try again"
        ) from exc
    if response.status_code != 200:
        raise ToolError(
            "WorkOS did not confirm your sign-in "
            f"(HTTP {response.status_code}); try signing in again"
        )
    data = response.json()
    return SignInIdentity(
        email=data.get("email"),
        email_verified=bool(data.get("email_verified")),
    )


class SignInMiddleware(Middleware):
    """Resolve each tool call's token to an allowed Brewery Pi user.

    ``session_factory`` is called per lookup (not captured once) so tests can
    swap the server's session factory after the middleware is built.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        tier: str,
        identity_lookup: IdentityLookup = fetch_workos_identity,
    ) -> None:
        self._session_factory = session_factory
        self._tier = tier
        self._identity_lookup = identity_lookup

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        user = await self.resolve_user()
        with signed_in_as(user):
            return await call_next(context)

    async def resolve_user(self) -> CurrentUser:
        """Return the caller, or raise ToolError saying why they can't."""
        token = get_access_token()
        subject = token.claims.get("sub") if token is not None else None
        if not subject:
            raise ToolError("sign in to Brewery Pi to use this connector")
        user = await anyio.to_thread.run_sync(self._known_user, subject)
        if user is None:
            identity = await self._identity_lookup(subject)
            user = await anyio.to_thread.run_sync(
                self._first_sign_in, subject, identity
            )
        return user

    def _known_user(self, subject: str) -> CurrentUser | None:
        with self._session_factory() as session:
            user = get_user_by_subject(session, subject)
            if user is None:
                return None
            return self._authorized(user)

    def _first_sign_in(
        self, subject: str, identity: SignInIdentity
    ) -> CurrentUser:
        with self._session_factory() as session:
            try:
                user = link_sign_in(
                    session,
                    subject,
                    identity.email,
                    identity.email_verified,
                )
            except PermissionDeniedError as exc:
                raise ToolError(str(exc)) from exc
            # Keep the link even if the tier refuses them: it is who they
            # are, and the refusal names the right next step either way.
            session.commit()
            return self._authorized(user)

    def _authorized(self, user) -> CurrentUser:
        try:
            authorize_tier(user, self._tier)
        except PermissionDeniedError as exc:
            raise ToolError(str(exc)) from exc
        return CurrentUser.from_user(user)
