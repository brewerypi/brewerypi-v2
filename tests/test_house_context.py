"""Tests for enterprise house context (storage, cap, tools, tiers)."""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.models import Enterprise
from brewerypi.services import (
    NotFoundError,
    ValidationError,
    create_enterprise,
    get_house_context,
    resolve_enterprise_id,
    set_house_context,
)
from brewerypi.services.enterprises import HOUSE_CONTEXT_MAX

_CONTEXT = "We say FV, not fermenter. Ales ferment at 66-70F."


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# -- service ---------------------------------------------------------------

def test_defaults_to_none(session):
    ent = create_enterprise(session, "NR", "New Realm")
    assert get_house_context(session, ent.id) is None


def test_set_and_get(session):
    ent = create_enterprise(session, "NR", "New Realm")
    set_house_context(session, ent.id, _CONTEXT)
    assert get_house_context(session, ent.id) == _CONTEXT


def test_replaces_rather_than_appends(session):
    ent = create_enterprise(session, "NR", "New Realm")
    set_house_context(session, ent.id, "first")
    set_house_context(session, ent.id, "second")
    assert get_house_context(session, ent.id) == "second"


def test_blank_clears(session):
    ent = create_enterprise(session, "NR", "New Realm")
    set_house_context(session, ent.id, _CONTEXT)
    set_house_context(session, ent.id, "   ")
    assert get_house_context(session, ent.id) is None


def test_cap_enforced(session):
    ent = create_enterprise(session, "NR", "New Realm")
    with pytest.raises(ValidationError) as exc:
        set_house_context(session, ent.id, "x" * (HOUSE_CONTEXT_MAX + 1))
    # the message should explain the cost, not just the limit
    assert "read into every conversation" in str(exc.value)


def test_cap_boundary_allowed(session):
    ent = create_enterprise(session, "NR", "New Realm")
    set_house_context(session, ent.id, "x" * HOUSE_CONTEXT_MAX)
    assert len(get_house_context(session, ent.id)) == HOUSE_CONTEXT_MAX


# -- enterprise resolution -------------------------------------------------

def test_resolves_the_only_enterprise(session):
    ent = create_enterprise(session, "NR", "New Realm")
    assert resolve_enterprise_id(session, None) == ent.id


def test_resolution_requires_id_when_several(session):
    create_enterprise(session, "NR", "New Realm")
    create_enterprise(session, "DB", "Deschutes")
    with pytest.raises(ValidationError):
        resolve_enterprise_id(session, None)


def test_resolution_with_none_present(session):
    with pytest.raises(NotFoundError):
        resolve_enterprise_id(session, None)


# -- tools -----------------------------------------------------------------

@pytest.fixture
def seeded(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'hc.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ent = Enterprise(abbreviation="NR", name="New Realm")
        s.add(ent)
        s.commit()
        eid = ent.id
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
    return eid


def test_tool_roundtrip_without_enterprise_id(seeded):
    assert mcp_server.get_house_context()["house_context"] is None
    preview = mcp_server.set_house_context(_CONTEXT)
    assert preview["confirm_required"] is True
    assert preview["current_house_context"] is None
    saved = mcp_server.set_house_context(_CONTEXT, confirm=True)
    assert saved["house_context"] == _CONTEXT
    assert mcp_server.get_house_context()["house_context"] == _CONTEXT


def test_preview_shows_current_before_overwrite(seeded):
    mcp_server.set_house_context("original", confirm=True)
    preview = mcp_server.set_house_context("replacement")
    assert preview["current_house_context"] == "original"
    assert preview["proposed_house_context"] == "replacement"
    # nothing changed without confirm
    assert mcp_server.get_house_context()["house_context"] == "original"


def test_tool_reports_cap_error(seeded):
    result = mcp_server.set_house_context(
        "x" * (HOUSE_CONTEXT_MAX + 1), confirm=True
    )
    assert "error" in result


def test_browse_hierarchy_carries_house_context(seeded):
    mcp_server.set_house_context(_CONTEXT, confirm=True)
    tree = mcp_server.browse_hierarchy()
    assert tree[0]["house_context"] == _CONTEXT


def test_read_on_operator_write_on_admin():
    async def names(server):
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    operator = asyncio.run(names(mcp_server.mcp))
    assert "get_house_context" in operator
    assert "set_house_context" not in operator
    admin_only = FastMCP("t")
    mcp_server._register_config_tools(admin_only)
    assert "set_house_context" in asyncio.run(names(admin_only))
