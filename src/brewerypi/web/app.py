"""The Brewery Pi web app: sign-in, and a page showing what you can reach.

Run it with the ``brewerypi-web`` command (or
``python -m brewerypi.web.app``). Sign-in settings are described in
``brewerypi.web.auth``; also read from the environment:

    DATABASE_URL  SQLAlchemy URL (default sqlite:///app.db)
    WEB_HOST      bind address (default 127.0.0.1)
    WEB_PORT      bind port (default 8002)

In deployment the process binds to localhost and a reverse proxy terminates
HTTPS; see the deploy guide.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from html import escape

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from brewerypi import access
from brewerypi.config import DATABASE_URL
from brewerypi.database import create_app_engine
from brewerypi.identity import CurrentUser, current_user
from brewerypi.models import Enterprise, Site
from brewerypi.services import PermissionDeniedError
from brewerypi.web.auth import (
    CodeExchange,
    Settings,
    SignInFailed,
    SignInMiddleware,
    authorization_url,
    complete_sign_in,
    exchange_code,
    logout_url,
)

SESSION_COOKIE = "brewerypi_session"
#: Idle limit: every request renews it, so a shift never gets signed out.
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60

_STYLE = """
:root { --bg: #eceff0; --surface: #ffffff; --ink: #162024; --muted: #59676d;
  --line: #c6cfd3; --accent: #0e6c61; --accent-ink: #ffffff; --bad: #9e2419; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #11171a; --surface: #192226; --ink: #e4ecee; --muted: #9babb1;
    --line: #34434a; --accent: #45b8a7; --accent-ink: #052019; --bad: #ff8c7e;
    color-scheme: dark; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-size: 17px;
  line-height: 1.4; font-family: "Segoe UI", system-ui, sans-serif; }
main { max-width: 640px; margin: 0 auto; padding: 24px 16px; display: grid;
  gap: 16px; }
h1 { margin: 0; font-size: 26px; } h2 { margin: 0 0 4px; font-size: 18px; }
p { margin: 0; } .muted { color: var(--muted); } .bad { color: var(--bad); }
section { background: var(--surface); border: 1px solid var(--line);
  border-radius: 10px; padding: 12px 16px; }
ul { margin: 0; padding-left: 20px; }
.btn { display: inline-block; min-height: 44px; padding: 10px 18px;
  border: 0; border-radius: 10px; background: var(--accent);
  color: var(--accent-ink); font: inherit; font-weight: 600;
  text-decoration: none; cursor: pointer; }
"""

_SIGN_OUT = (
    '<form method="post" action="/logout">'
    '<button class="btn" type="submit">Sign out</button></form>'
)
_SIGN_IN = '<p><a class="btn" href="/login">Sign in</a></p>'


class _NotSignedIn(Exception):
    """The page needs a signed-in user, and there isn't one."""


class _Refused(Exception):
    """They signed in, but Brewery Pi doesn't let them in."""


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """A whole page, never cached: tablets on the floor are shared."""
    return HTMLResponse(
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, '
        'initial-scale=1">'
        f"<title>{escape(title)} - Brewery Pi</title>"
        f"<style>{_STYLE}</style></head>"
        f"<body><main><h1>{escape(title)}</h1>{body}</main></body></html>",
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _refused_page(reason: str) -> HTMLResponse:
    return _page(
        "You can't use Brewery Pi yet",
        f'<p class="bad">{escape(reason)}</p>{_SIGN_OUT}',
        status_code=403,
    )


def _signed_in_user(request: Request) -> CurrentUser:
    """The page's user, or an exception the app turns into the right page."""
    refusal = getattr(request.state, "refusal", None)
    if refusal:
        raise _Refused(refusal)
    user = current_user()
    if user is None:
        raise _NotSignedIn
    return user


def create_app(
    settings: Settings,
    session_factory: Callable[[], Session],
    exchange: CodeExchange = exchange_code,
) -> FastAPI:
    """Build the app; everything it talks to is passed in, for the tests."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # Added inside-out: the session must exist before sign-in reads it.
    app.add_middleware(SignInMiddleware, session_factory=session_factory)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.is_https,
    )

    @app.exception_handler(_NotSignedIn)
    def _send_to_sign_in(request: Request, exc: _NotSignedIn):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(_Refused)
    def _show_refusal(request: Request, exc: _Refused):
        return _refused_page(str(exc))

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/login")
    def login(request: Request):
        state = secrets.token_urlsafe(32)
        request.session["state"] = state
        return RedirectResponse(
            authorization_url(settings, state), status_code=303
        )

    @app.get("/callback")
    def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        expected = request.session.pop("state", None)
        if (
            not expected
            or not state
            or not secrets.compare_digest(expected, state)
        ):
            return _page(
                "That sign-in has expired",
                "<p>Please start again.</p>" + _SIGN_IN,
                status_code=400,
            )
        if error or not code:
            return _page(
                "Sign-in didn't finish",
                "<p>Please try again.</p>" + _SIGN_IN,
                status_code=400,
            )
        try:
            sign_in = exchange(settings, code)
        except SignInFailed as exc:
            return _page(
                "Sign-in didn't finish",
                f"<p>{escape(str(exc))}</p>" + _SIGN_IN,
                status_code=502,
            )
        # A new identity starts a new session, whoever was signed in before.
        request.session.clear()
        if sign_in.session_id:
            request.session["session_id"] = sign_in.session_id
        try:
            complete_sign_in(session_factory, sign_in)
        except PermissionDeniedError as exc:
            # No subject in the cookie, so this grants nothing. The session
            # id stays so that signing out lets them try another account.
            return _refused_page(str(exc))
        request.session["subject"] = sign_in.subject
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        session_id = request.session.get("session_id")
        request.session.clear()
        target = (
            logout_url(settings, session_id) if session_id else "/signed-out"
        )
        return RedirectResponse(target, status_code=303)

    @app.get("/signed-out")
    def signed_out():
        return _page("You're signed out", _SIGN_IN)

    @app.get("/")
    def home(user: CurrentUser = Depends(_signed_in_user)):
        with session_factory() as session:
            enterprises = access.visible(
                session,
                user,
                session.scalars(select(Enterprise).order_by(Enterprise.name)),
            )
            sites = access.visible(
                session,
                user,
                session.scalars(select(Site).order_by(Site.name)),
            )
            sections = []
            for enterprise in enterprises:
                names = [
                    f"<li>{escape(s.name)}</li>"
                    for s in sites
                    if s.enterprise_id == enterprise.id
                ]
                listing = (
                    f"<ul>{''.join(names)}</ul>"
                    if names
                    else '<p class="muted">No sites yet.</p>'
                )
                sections.append(
                    f"<section><h2>{escape(enterprise.name)}</h2>"
                    f"{listing}</section>"
                )
        if not sections:
            sections.append(
                '<p class="muted">You have no breweries to see yet.</p>'
            )
        return _page(
            "Brewery Pi",
            f"<p>Signed in as <strong>{escape(user.email)}</strong></p>"
            + "".join(sections)
            + _SIGN_OUT,
        )

    return app


def main() -> None:
    """Start the web app, refusing to start without sign-in configured."""
    app = create_app(
        Settings.from_env(),
        sessionmaker(create_app_engine(DATABASE_URL)),
    )
    uvicorn.run(
        app,
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8002")),
    )


if __name__ == "__main__":
    main()
