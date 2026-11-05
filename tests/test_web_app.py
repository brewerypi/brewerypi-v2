"""Tests for the web app's sign-in flow, from the browser's side.

The WorkOS exchange is a fake, so nothing here talks to WorkOS. Each test
drives the real routes and middleware against a real database file.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from brewerypi.database import Base
from brewerypi.models import Enterprise, Site, User
from brewerypi.services import (
    create_membership,
    create_user,
    get_user_by_email,
    get_user_by_subject,
    update_user,
)
from brewerypi.web import app as web_app
from brewerypi.web.app import SESSION_COOKIE, create_app
from brewerypi.web.auth import Settings, SignIn, SignInFailed

_SETTINGS = Settings(
    client_id="client_123",
    api_key="sk_test_123",
    base_url="https://app.example.com",
    session_secret="x" * 32,
)
_BASE_URL = "https://app.example.com"


class FakeWorkOS:
    """Stands in for the code exchange; ``sign_in`` is who comes back."""

    def __init__(self):
        self.sign_in = _identity("op@brewery.com")
        self.error = None
        self.codes = []

    def __call__(self, settings, code):
        self.codes.append(code)
        if self.error:
            raise self.error
        return self.sign_in


def _identity(email, subject="user_1", verified=True, session_id="session_1"):
    return SignIn(
        subject=subject,
        email=email,
        email_verified=verified,
        session_id=session_id,
    )


@pytest.fixture
def factory(tmp_path):
    """Two companies, and an operator who belongs only to the first."""
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        mine = Enterprise(abbreviation="M", name="My Brewery")
        other = Enterprise(abbreviation="O", name="Other Brewery")
        session.add_all([mine, other])
        session.flush()
        session.add_all(
            [
                Site(
                    enterprise_id=mine.id,
                    abbreviation="SPR",
                    name="Springfield",
                    timezone="UTC",
                ),
                Site(
                    enterprise_id=mine.id,
                    abbreviation="ATL",
                    name="Atlanta",
                    timezone="UTC",
                ),
                Site(
                    enterprise_id=other.id,
                    abbreviation="SEC",
                    name="Secret Site",
                    timezone="UTC",
                ),
            ]
        )
        session.flush()
        operator = create_user(session, "op@brewery.com")
        create_membership(session, operator.id, mine.id, "operator")
        create_user(session, "nobody@brewery.com")
        session.commit()
    return factory


@pytest.fixture
def workos():
    return FakeWorkOS()


@pytest.fixture
def client(factory, workos):
    app = create_app(_SETTINGS, factory, exchange=workos)
    return TestClient(app, base_url=_BASE_URL, follow_redirects=False)


def _begin(client) -> str:
    """Start a sign-in and return the state AuthKit would send back."""
    response = client.get("/login")
    assert response.status_code == 303
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def _sign_in(client):
    return client.get(
        "/callback", params={"code": "the-code", "state": _begin(client)}
    )


def test_healthz_needs_no_sign_in(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_a_visitor_is_sent_to_sign_in(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_sends_the_browser_to_authkit(client):
    location = client.get("/login").headers["location"]
    assert location.startswith(
        "https://api.workos.com/user_management/authorize?"
    )
    assert "provider=authkit" in location
    assert "client_id=client_123" in location


def test_signing_in_links_the_user_and_opens_the_home_page(
    client, factory, workos
):
    response = _sign_in(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert workos.codes == ["the-code"]
    with factory() as session:
        linked = get_user_by_subject(session, "user_1")
        assert linked.email == "op@brewery.com"

    home = client.get("/")
    assert home.status_code == 200
    assert "op@brewery.com" in home.text


def test_the_home_page_shows_only_what_the_user_may_reach(client):
    _sign_in(client)
    home = client.get("/").text
    assert "My Brewery" in home
    assert "Springfield" in home
    assert "Atlanta" in home
    assert "Other Brewery" not in home
    assert "Secret Site" not in home


def test_a_site_restricted_user_sees_only_their_site(factory, client, workos):
    with factory() as session:
        user = create_user(session, "one-site@brewery.com")
        site = session.query(Site).filter_by(abbreviation="SPR").one()
        create_membership(
            session,
            user.id,
            site.enterprise_id,
            "operator",
            site_ids=[site.id],
        )
        session.commit()
    workos.sign_in = _identity("one-site@brewery.com", subject="user_2")
    _sign_in(client)
    home = client.get("/").text
    assert "Springfield" in home
    assert "Atlanta" not in home


def test_a_wrong_state_is_refused_before_asking_workos(client, workos):
    _begin(client)
    response = client.get(
        "/callback", params={"code": "the-code", "state": "forged"}
    )
    assert response.status_code == 400
    assert workos.codes == []
    assert client.get("/").status_code == 303


def test_a_callback_nobody_started_is_refused(client, workos):
    response = client.get(
        "/callback", params={"code": "the-code", "state": "anything"}
    )
    assert response.status_code == 400
    assert workos.codes == []


def test_a_state_cannot_be_used_twice(client, workos):
    state = _begin(client)
    params = {"code": "the-code", "state": state}
    assert client.get("/callback", params=params).status_code == 303
    assert client.get("/callback", params=params).status_code == 400
    assert workos.codes == ["the-code"]


def test_an_error_from_authkit_says_try_again(client, workos):
    response = client.get(
        "/callback",
        params={"error": "access_denied", "state": _begin(client)},
    )
    assert response.status_code == 400
    assert "Sign-in didn" in response.text
    assert workos.codes == []


def test_a_failed_exchange_is_reported_and_signs_nobody_in(client, workos):
    workos.error = SignInFailed("could not reach WorkOS")
    response = _sign_in(client)
    assert response.status_code == 502
    assert "could not reach WorkOS" in response.text
    assert client.get("/").status_code == 303


@pytest.mark.parametrize(
    "identity, reason",
    [
        (_identity("stranger@brewery.com"), "is not a Brewery Pi user"),
        (_identity("op@brewery.com", verified=False), "no verified email"),
        (_identity("nobody@brewery.com"), "has not been given access"),
    ],
)
def test_someone_not_allowed_in_is_told_why(client, workos, identity, reason):
    workos.sign_in = identity
    response = _sign_in(client)
    assert response.status_code == 403
    assert reason in response.text
    assert client.get("/").status_code == 303


def test_a_refused_message_is_escaped(client, workos):
    workos.sign_in = _identity("<script>x</script>@brewery.com")
    response = _sign_in(client)
    assert response.status_code == 403
    assert "<script>x" not in response.text


def test_someone_refused_can_sign_out_and_try_another_account(client, workos):
    workos.sign_in = _identity("stranger@brewery.com")
    assert "Sign out" in _sign_in(client).text
    response = client.post("/logout")
    assert "session_id=session_1" in response.headers["location"]


def test_deactivating_someone_takes_effect_on_their_next_request(
    client, factory
):
    _sign_in(client)
    assert client.get("/").status_code == 200
    with factory() as session:
        user = get_user_by_email(session, "op@brewery.com")
        update_user(session, user.id, is_active=False)
        session.commit()
    response = client.get("/")
    assert response.status_code == 403
    assert "deactivated" in response.text
    assert "My Brewery" not in response.text


def test_removing_access_takes_effect_on_their_next_request(client, factory):
    _sign_in(client)
    with factory() as session:
        user = get_user_by_email(session, "op@brewery.com")
        session.delete(user.memberships[0])
        session.commit()
    response = client.get("/")
    assert response.status_code == 403
    assert "My Brewery" not in response.text


def test_a_session_for_a_deleted_user_goes_back_to_sign_in(client, factory):
    _sign_in(client)
    with factory() as session:
        session.delete(session.query(User).filter_by(
            email="op@brewery.com"
        ).one())
        session.commit()
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_signing_out_ends_both_sessions(client):
    _sign_in(client)
    response = client.post("/logout")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(
        "https://api.workos.com/user_management/sessions/logout?"
    )
    assert "session_id=session_1" in location
    assert "signed-out" in location
    assert client.get("/").status_code == 303


def test_signing_out_without_a_workos_session_stays_here(client, workos):
    workos.sign_in = _identity("op@brewery.com", session_id=None)
    _sign_in(client)
    response = client.post("/logout")
    assert response.headers["location"] == "/signed-out"
    assert "signed out" in client.get("/signed-out").text


def test_signing_out_needs_a_post(client):
    _sign_in(client)
    assert client.get("/logout").status_code == 405
    assert client.get("/").status_code == 200


def test_the_cookie_is_locked_down(client):
    response = _sign_in(client)
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(SESSION_COOKIE)
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_a_cookie_signed_with_another_key_is_ignored(factory, workos, client):
    other_settings = Settings(
        client_id="client_123",
        api_key="sk_test_123",
        base_url=_BASE_URL,
        session_secret="y" * 32,
    )
    other = TestClient(
        create_app(other_settings, factory, exchange=workos),
        base_url=_BASE_URL,
        follow_redirects=False,
    )
    _sign_in(other)
    forged = other.cookies.get(SESSION_COOKIE)
    client.cookies.set(SESSION_COOKIE, forged)
    assert client.get("/").status_code == 303


def test_pages_are_never_cached(client):
    _sign_in(client)
    assert client.get("/").headers["cache-control"] == "no-store"


def test_pages_recheck_with_the_server_when_restored_by_back(client):
    _sign_in(client)
    home = client.get("/").text
    assert 'addEventListener("pageshow"' in home
    assert "event.persisted" in home
    assert "location.reload()" in home


def test_no_api_docs_are_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_main_refuses_to_start_without_sign_in_configured(monkeypatch):
    for name in (
        "WORKOS_CLIENT_ID",
        "WORKOS_API_KEY",
        "WEB_BASE_URL",
        "WEB_SESSION_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        web_app.main()
