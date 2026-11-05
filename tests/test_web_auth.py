"""Tests for web sign-in settings and the exchange with WorkOS.

``httpx.post`` is replaced, so nothing here talks to WorkOS. The sign-in flow
itself, including the middleware, is exercised in ``test_web_app.py``.
"""

import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from brewerypi.web import auth
from brewerypi.web.auth import (
    Settings,
    SignInFailed,
    authorization_url,
    exchange_code,
    logout_url,
    session_id_from_token,
)

_ENV = {
    "WORKOS_CLIENT_ID": "client_123",
    "WORKOS_API_KEY": "sk_test_123",
    "WEB_BASE_URL": "https://app.example.com/",
    "WEB_SESSION_SECRET": "x" * 32,
}
_SETTINGS = Settings(
    client_id="client_123",
    api_key="sk_test_123",
    base_url="https://app.example.com",
    session_secret="x" * 32,
)


def _token(claims) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode())
    return f"header.{payload.decode().rstrip('=')}.signature"


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


@pytest.fixture
def env(monkeypatch):
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_settings_read_the_environment(env):
    settings = Settings.from_env()
    assert settings.client_id == "client_123"
    assert settings.base_url == "https://app.example.com"
    assert settings.redirect_uri == "https://app.example.com/callback"
    assert settings.is_https


@pytest.mark.parametrize("missing", sorted(_ENV))
def test_settings_refuse_to_start_incomplete(env, missing):
    env.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        Settings.from_env()


def test_settings_refuse_a_short_session_secret(env):
    env.setenv("WEB_SESSION_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="at least 32"):
        Settings.from_env()


@pytest.mark.parametrize(
    "name, value",
    [
        ("WORKOS_CLIENT_ID", "client_REPLACE_ME"),
        ("WORKOS_API_KEY", "sk_REPLACE_ME"),
        ("WEB_SESSION_SECRET", "PASTE_THE_GENERATED_VALUE_FROM_THE_GUIDE"),
    ],
)
def test_settings_refuse_the_deploy_guides_placeholders(env, name, value):
    env.setenv(name, value)
    with pytest.raises(RuntimeError, match=f"placeholder: {name}"):
        Settings.from_env()


def test_authorization_url_asks_authkit_for_a_code():
    query = _query(authorization_url(_SETTINGS, "the-state"))
    assert query == {
        "client_id": "client_123",
        "provider": "authkit",
        "redirect_uri": "https://app.example.com/callback",
        "response_type": "code",
        "state": "the-state",
    }


def test_logout_url_names_the_session_and_where_to_return():
    url = logout_url(_SETTINGS, "session_1")
    assert url.startswith(
        "https://api.workos.com/user_management/sessions/logout?"
    )
    assert _query(url) == {
        "session_id": "session_1",
        "return_to": "https://app.example.com/signed-out",
    }


def test_session_id_comes_from_the_access_tokens_sid_claim():
    assert session_id_from_token(_token({"sid": "session_1"})) == "session_1"
    assert session_id_from_token(_token({"sub": "user_1"})) is None


@pytest.mark.parametrize(
    "token", [None, "", "not-a-token", "a.!!!.c", _token(["not", "claims"])]
)
def test_session_id_is_none_for_an_unreadable_token(token):
    assert session_id_from_token(token) is None


def test_exchange_code_returns_the_confirmed_identity(monkeypatch):
    sent = {}

    def post(url, json, timeout):
        sent.update(url=url, json=json)
        return httpx.Response(
            200,
            json={
                "user": {
                    "id": "user_1",
                    "email": "sam@brewery.com",
                    "email_verified": True,
                },
                "access_token": _token({"sid": "session_1"}),
            },
        )

    monkeypatch.setattr(auth.httpx, "post", post)
    sign_in = exchange_code(_SETTINGS, "the-code")

    assert sent["url"].endswith("/user_management/authenticate")
    assert sent["json"] == {
        "client_id": "client_123",
        "client_secret": "sk_test_123",
        "grant_type": "authorization_code",
        "code": "the-code",
    }
    assert sign_in.subject == "user_1"
    assert sign_in.email == "sam@brewery.com"
    assert sign_in.email_verified is True
    assert sign_in.session_id == "session_1"


def test_exchange_code_reports_an_unreachable_workos(monkeypatch):
    def post(url, json, timeout):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(auth.httpx, "post", post)
    with pytest.raises(SignInFailed, match="could not reach WorkOS"):
        exchange_code(_SETTINGS, "the-code")


def test_exchange_code_reports_a_refusal_without_its_body(monkeypatch):
    def post(url, json, timeout):
        return httpx.Response(400, json={"error": "secret detail"})

    monkeypatch.setattr(auth.httpx, "post", post)
    with pytest.raises(SignInFailed, match="HTTP 400") as caught:
        exchange_code(_SETTINGS, "the-code")
    assert "secret detail" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"no": "user"}),
        httpx.Response(200, json={"user": {"email": "no-id@brewery.com"}}),
    ],
)
def test_exchange_code_reports_an_unexpected_answer(monkeypatch, response):
    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: response)
    with pytest.raises(SignInFailed, match="unexpected answer"):
        exchange_code(_SETTINGS, "the-code")
