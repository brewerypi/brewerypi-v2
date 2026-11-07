"""The Brewery Pi web app: sign-in, and the pages behind it.

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

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from brewerypi.config import DATABASE_URL
from brewerypi.database import create_app_engine
from brewerypi.services import NotFoundError, PermissionDeniedError
from brewerypi.web import batch_actions, batches, readings
from brewerypi.web.auth import (
    CodeExchange,
    NotSignedIn,
    Refused,
    Settings,
    SignInFailed,
    SignInMiddleware,
    authorization_url,
    complete_sign_in,
    exchange_code,
    logout_url,
)
from brewerypi.web.rendering import message_page

SESSION_COOKIE = "brewerypi_session"
#: Idle limit: every request renews it, so a shift never gets signed out.
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60


def _refused_page(request: Request, reason: str):
    return message_page(
        request,
        "You can't use Brewery Pi yet",
        reason,
        status_code=403,
        sign_out=True,
        tone="bad",
    )


def _not_found_page(request: Request):
    """The one answer for anything missing or out of the person's scope."""
    return message_page(
        request,
        "We can't find that",
        "It may not exist, or you may not have access to it. Ask a Brewery "
        "Pi administrator if you expected to see it.",
        status_code=404,
    )


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

    @app.exception_handler(NotSignedIn)
    def _send_to_sign_in(request: Request, exc: NotSignedIn):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Refused)
    def _show_refusal(request: Request, exc: Refused):
        return _refused_page(request, str(exc))

    @app.exception_handler(NotFoundError)
    def _show_not_found(request: Request, exc: NotFoundError):
        return _not_found_page(request)

    @app.exception_handler(PermissionDeniedError)
    def _show_forbidden(request: Request, exc: PermissionDeniedError):
        return message_page(
            request,
            "You can't do that here",
            str(exc),
            status_code=403,
            tone="bad",
        )

    @app.exception_handler(RequestValidationError)
    def _show_bad_address(request: Request, exc: RequestValidationError):
        return _not_found_page(request)

    @app.exception_handler(HTTPException)
    def _show_http_error(request: Request, exc: HTTPException):
        if exc.status_code == 404:
            return _not_found_page(request)
        return message_page(
            request,
            "That didn't work",
            status_code=exc.status_code,
        )

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
            return message_page(
                request,
                "That sign-in has expired",
                "Please start again.",
                status_code=400,
                sign_in=True,
            )
        if error or not code:
            return message_page(
                request,
                "Sign-in didn't finish",
                "Please try again.",
                status_code=400,
                sign_in=True,
            )
        try:
            sign_in = exchange(settings, code)
        except SignInFailed as exc:
            return message_page(
                request,
                "Sign-in didn't finish",
                str(exc),
                status_code=502,
                sign_in=True,
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
            return _refused_page(request, str(exc))
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
    def signed_out(request: Request):
        return message_page(request, "You're signed out", sign_in=True)

    app.include_router(batches.create_router(session_factory))
    app.include_router(batch_actions.create_router(session_factory))
    app.include_router(readings.create_router(session_factory))
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
