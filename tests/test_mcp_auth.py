"""Tests for MCP sign-in: configuration, and the allowlist middleware.

The middleware is driven directly with a stand-in for the verified token and
a fake WorkOS lookup, so nothing here talks to WorkOS. Token verification
itself is FastMCP's ``AuthKitProvider`` and is not re-tested.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_auth, mcp_server
from brewerypi.database import Base
from brewerypi.identity import current_user
from brewerypi.models import Enterprise, User
from brewerypi.services import create_membership, create_user

_AUTHKIT_ENV = {
    "AUTHKIT_DOMAIN": "https://example.authkit.app",
    "MCP_BASE_URL": "https://mcp.example.com",
    "WORKOS_API_KEY": "sk_test_123",
}


@pytest.fixture
def db(tmp_path):
    """A file DB with one enterprise and four users of differing access."""
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E1", name="Ent One")
        session.add(ent)
        session.flush()
        operator = create_user(session, "op@example.com",
                               timezone="America/Denver")
        create_membership(session, operator.id, ent.id, "operator")
        admin = create_user(session, "admin@example.com")
        create_membership(session, admin.id, ent.id, "admin")
        create_user(session, "nobody@example.com")
        gone = create_user(session, "gone@example.com", is_superuser=True)
        gone.is_active = False
        session.commit()
    return sessionmaker(engine)


def _token(monkeypatch, subject):
    token = None if subject is None else SimpleNamespace(
        claims={"sub": subject}
    )
    monkeypatch.setattr(mcp_auth, "get_access_token", lambda: token)


def _directory(entries):
    """A fake WorkOS lookup: subject -> (email, verified); counts calls."""
    calls = []

    async def lookup(subject):
        calls.append(subject)
        email, verified = entries[subject]
        return mcp_auth.SignInIdentity(email=email, email_verified=verified)

    return lookup, calls


def _middleware(db, tier, lookup):
    return mcp_auth.SignInMiddleware(db, tier, identity_lookup=lookup)


# -- configuration ---------------------------------------------------------

def test_auth_mode_defaults_to_authkit_and_rejects_typos(monkeypatch):
    monkeypatch.delenv("MCP_AUTH", raising=False)
    assert mcp_auth.auth_mode() == "authkit"
    monkeypatch.setenv("MCP_AUTH", " None ")
    assert mcp_auth.auth_mode() == "none"
    monkeypatch.setenv("MCP_AUTH", "off")
    with pytest.raises(RuntimeError):
        mcp_auth.auth_mode()


def test_provider_refuses_missing_settings(monkeypatch):
    for name in _AUTHKIT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTHKIT_DOMAIN", _AUTHKIT_ENV["AUTHKIT_DOMAIN"])
    with pytest.raises(RuntimeError, match="MCP_BASE_URL, WORKOS_API_KEY"):
        mcp_auth.build_auth_provider()


def test_provider_builds_from_the_environment(monkeypatch):
    for name, value in _AUTHKIT_ENV.items():
        monkeypatch.setenv(name, value)
    provider = mcp_auth.build_auth_provider()
    assert provider.authkit_domain == _AUTHKIT_ENV["AUTHKIT_DOMAIN"]


@pytest.mark.parametrize(
    "domain",
    ["example.authkit.app", " https://example.authkit.app/ "],
)
def test_provider_accepts_a_bare_or_slashed_domain(monkeypatch, domain):
    """The dashboard shows the domain without a scheme; take it as shown."""
    for name, value in _AUTHKIT_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AUTHKIT_DOMAIN", domain)
    provider = mcp_auth.build_auth_provider()
    assert provider.authkit_domain == "https://example.authkit.app"


@pytest.fixture
def fake_server(monkeypatch):
    """Swap the module's server for a blank one whose run() is recorded."""
    server = FastMCP("test")
    runs = []
    monkeypatch.setattr(server, "run", lambda **kw: runs.append(kw))
    monkeypatch.setattr(mcp_server, "mcp", server)
    return server, runs


def test_main_refuses_to_start_unconfigured(monkeypatch, fake_server):
    server, runs = fake_server
    monkeypatch.delenv("MCP_AUTH", raising=False)
    for name in _AUTHKIT_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        mcp_server.main()
    assert runs == []


def test_main_refuses_an_unknown_tier(monkeypatch, fake_server):
    monkeypatch.setenv("MCP_ROLE", "Admin")
    monkeypatch.setenv("MCP_AUTH", "none")
    with pytest.raises(SystemExit):
        mcp_server.main()


def test_main_attaches_sign_in(monkeypatch, fake_server):
    server, runs = fake_server
    monkeypatch.delenv("MCP_AUTH", raising=False)
    for name, value in _AUTHKIT_ENV.items():
        monkeypatch.setenv(name, value)
    mcp_server.main()
    assert server.auth is not None
    assert any(
        isinstance(m, mcp_auth.SignInMiddleware) for m in server.middleware
    )
    assert len(runs) == 1


def test_main_runs_open_only_when_told_to(monkeypatch, fake_server):
    server, runs = fake_server
    monkeypatch.setenv("MCP_AUTH", "none")
    mcp_server.main()
    assert server.auth is None
    assert len(runs) == 1


# -- the allowlist ---------------------------------------------------------

def test_no_token_is_refused(monkeypatch, db):
    _token(monkeypatch, None)
    lookup, _ = _directory({})
    with pytest.raises(ToolError, match="sign in"):
        asyncio.run(_middleware(db, "operator", lookup).resolve_user())


def test_first_sign_in_links_then_matches_on_subject(monkeypatch, db):
    _token(monkeypatch, "user_op")
    lookup, calls = _directory({"user_op": ("OP@example.com", True)})
    mw = _middleware(db, "operator", lookup)

    user = asyncio.run(mw.resolve_user())
    assert user.email == "op@example.com"
    assert user.timezone == "America/Denver"
    with db() as session:
        stored = session.query(User).filter_by(email="op@example.com").one()
        assert stored.auth_subject == "user_op"

    asyncio.run(mw.resolve_user())
    assert calls == ["user_op"]  # WorkOS asked once, not per call


def test_stranger_and_unverified_are_refused(monkeypatch, db):
    lookup, _ = _directory({
        "user_x": ("stranger@example.com", True),
        "user_op": ("op@example.com", False),
    })
    mw = _middleware(db, "operator", lookup)
    _token(monkeypatch, "user_x")
    with pytest.raises(ToolError, match="not a Brewery Pi user"):
        asyncio.run(mw.resolve_user())
    _token(monkeypatch, "user_op")
    with pytest.raises(ToolError, match="verified"):
        asyncio.run(mw.resolve_user())


def test_user_without_membership_is_refused(monkeypatch, db):
    _token(monkeypatch, "user_n")
    lookup, _ = _directory({"user_n": ("nobody@example.com", True)})
    with pytest.raises(ToolError, match="access to a brewery"):
        asyncio.run(_middleware(db, "operator", lookup).resolve_user())


def test_deactivated_user_is_refused(monkeypatch, db):
    _token(monkeypatch, "user_g")
    lookup, _ = _directory({"user_g": ("gone@example.com", True)})
    with pytest.raises(ToolError, match="deactivated"):
        asyncio.run(_middleware(db, "admin", lookup).resolve_user())


def test_admin_tier_admits_only_admins(monkeypatch, db):
    lookup, _ = _directory({
        "user_op": ("op@example.com", True),
        "user_ad": ("admin@example.com", True),
    })
    mw = _middleware(db, "admin", lookup)
    _token(monkeypatch, "user_op")
    with pytest.raises(ToolError, match="operator connector"):
        asyncio.run(mw.resolve_user())
    _token(monkeypatch, "user_ad")
    assert asyncio.run(mw.resolve_user()).email == "admin@example.com"


def test_tool_call_runs_signed_in(monkeypatch, db):
    _token(monkeypatch, "user_op")
    lookup, _ = _directory({"user_op": ("op@example.com", True)})
    mw = _middleware(db, "operator", lookup)
    seen = []

    async def call_next(context):
        seen.append(current_user())
        return "result"

    assert asyncio.run(mw.on_call_tool(None, call_next)) == "result"
    assert seen[0].email == "op@example.com"
    assert current_user() is None


def test_refused_call_never_reaches_the_tool(monkeypatch, db):
    _token(monkeypatch, "user_n")
    lookup, _ = _directory({"user_n": ("nobody@example.com", True)})
    mw = _middleware(db, "operator", lookup)

    async def call_next(context):
        raise AssertionError("tool ran for a refused user")

    with pytest.raises(ToolError):
        asyncio.run(mw.on_call_tool(None, call_next))


# -- the WorkOS email lookup -----------------------------------------------

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _workos(monkeypatch, handler):
    """Route the lookup's HTTP through ``handler`` instead of the network."""
    monkeypatch.setenv("WORKOS_API_KEY", "sk_test_123")
    monkeypatch.setattr(
        mcp_auth.httpx,
        "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(handler), **kw
        ),
    )


def test_workos_lookup_reads_the_users_email(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json={"email": "op@example.com", "email_verified": True}
        )

    _workos(monkeypatch, handler)
    identity = asyncio.run(mcp_auth.fetch_workos_identity("user_01/x"))
    assert identity == mcp_auth.SignInIdentity("op@example.com", True)
    # the subject is one path segment, even with a "/" in it
    assert seen[0].url.raw_path == b"/user_management/users/user_01%2Fx"
    assert seen[0].headers["Authorization"] == "Bearer sk_test_123"


def test_workos_lookup_failures_become_tool_errors(monkeypatch):
    _workos(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(ToolError, match="HTTP 404"):
        asyncio.run(mcp_auth.fetch_workos_identity("user_01"))

    def unreachable(request):
        raise httpx.ConnectError("down")

    _workos(monkeypatch, unreachable)
    with pytest.raises(ToolError, match="could not reach WorkOS"):
        asyncio.run(mcp_auth.fetch_workos_identity("user_01"))
