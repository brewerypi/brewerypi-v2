"""Tests that every MCP tool is scoped to the caller's companies and sites.

Two companies share one database: Atlas (two sites, North and South) and
Borealis (one site). Each test signs in as someone with a particular grant
and calls the tools directly, as the sign-in middleware would after
resolving them.
"""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.identity import CurrentUser, signed_in_as
from brewerypi.models import Area, Enterprise, Site, Tag
from brewerypi.services import (
    create_element,
    create_element_template,
    create_event_frame_template,
    create_membership,
    create_user,
    get_user_by_email,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'scoping.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(mcp_server, "_Session", factory)
    ids = {}
    with Session(engine) as session:
        for ent_key, sites in (("atlas", ("north", "south")),
                               ("borealis", ("east",))):
            ent = Enterprise(abbreviation=ent_key[:3].upper(), name=ent_key)
            session.add(ent)
            session.flush()
            ids[ent_key] = ent.id
            for site_key in sites:
                site = Site(
                    abbreviation=site_key[:3].upper(),
                    name=site_key,
                    timezone="UTC",
                    enterprise_id=ent.id,
                )
                session.add(site)
                session.flush()
                area = Area(abbreviation="CEL", name="Cellar", site_id=site.id)
                session.add(area)
                session.flush()
                tag = Tag(name=f"{site_key} temp", area_id=area.id)
                session.add(tag)
                template = create_element_template(
                    session, site.id, "Fermenter"
                )
                element = create_element(
                    session, template.id, "FV01", tag_area_id=area.id
                )
                batch = create_event_frame_template(
                    session, template.id, "Fermentation"
                )
                session.flush()
                ids.update(
                    {
                        site_key: site.id,
                        f"{site_key}_area": area.id,
                        f"{site_key}_tag": tag.id,
                        f"{site_key}_fv": element.id,
                        f"{site_key}_ferm": batch.id,
                    }
                )
        people = {
            "north_operator": ("atlas", "operator", ["north"]),
            "north_admin": ("atlas", "admin", ["north"]),
            "atlas_admin": ("atlas", "admin", None),
            "borealis_operator": ("borealis", "operator", None),
        }
        for key, (ent_key, role, sites) in people.items():
            user = create_user(session, f"{key}@brewery.com")
            create_membership(
                session,
                user.id,
                ids[ent_key],
                role,
                None if sites is None else [ids[s] for s in sites],
            )
        session.commit()

    def as_(key):
        with factory() as session:
            snapshot = CurrentUser.from_user(
                get_user_by_email(session, f"{key}@brewery.com")
            )
        return signed_in_as(snapshot)

    return ids, as_


# -- reading --------------------------------------------------------------

def test_another_company_is_invisible(world):
    ids, as_ = world
    with as_("borealis_operator"):
        assert [e["name"] for e in mcp_server.list_enterprises()] == [
            "borealis"
        ]
        assert [s["name"] for s in mcp_server.list_sites()] == ["east"]
        assert mcp_server.list_areas(ids["north"]) == []
        assert mcp_server.get_tag_values(ids["north_tag"]) == {
            "error": f"no tag with id {ids['north_tag']}"
        }


def test_a_site_restriction_hides_the_other_sites(world):
    ids, as_ = world
    with as_("north_operator"):
        tree = mcp_server.browse_hierarchy()
        shown = [(e["name"], [s["name"] for s in e["sites"]]) for e in tree]
        assert shown == [("atlas", ["north"])]
        assert [e["id"] for e in mcp_server.list_elements()] == [
            ids["north_fv"]
        ]
        assert "error" in mcp_server.get_element(ids["south_fv"])
        assert "error" not in mcp_server.get_tag_values(ids["north_tag"])


def test_unknown_ids_still_get_the_tools_own_answer(world):
    _, as_ = world
    with as_("north_operator"):
        assert mcp_server.get_tag_values(99999) == {
            "error": "no tag with id 99999"
        }


def test_nobody_signed_in_sees_everything(world):
    assert len(mcp_server.list_enterprises()) == 2


# -- operating ------------------------------------------------------------

def test_operators_record_and_run_batches_only_at_their_sites(world):
    ids, as_ = world
    with as_("north_operator"):
        reading = mcp_server.record_tag_value(ids["north_tag"], value=12.5)
        assert "error" not in reading
        assert "error" in mcp_server.record_tag_value(
            ids["south_tag"], value=12.5
        )
        frame = mcp_server.create_event_frame(
            ids["north_fv"], ids["north_ferm"], "B1"
        )
        assert "error" not in frame
        assert "error" not in mcp_server.close_event_frame(frame["id"])
        assert "error" in mcp_server.create_event_frame(
            ids["south_fv"], ids["south_ferm"], "B1"
        )


def test_a_batch_cannot_name_another_sites_batch(world):
    ids, as_ = world
    south = mcp_server.create_event_frame(
        ids["south_fv"], ids["south_ferm"], "South batch"
    )
    with as_("north_operator"):
        result = mcp_server.create_event_frame(
            ids["north_fv"],
            ids["north_ferm"],
            "Transfer",
            source_event_frame_id=south["id"],
        )
    assert result == {"error": f"no event frame with id {south['id']}"}


# -- configuring ----------------------------------------------------------

def test_operators_cannot_configure(world):
    ids, as_ = world
    with as_("north_operator"):
        result = mcp_server.create_area(ids["north"], "PK", "Packaging")
    assert "not an administrator at this site" in result["error"]


def test_a_site_admin_configures_only_their_site(world):
    ids, as_ = world
    with as_("north_admin"):
        assert "error" not in mcp_server.create_area(
            ids["north"], "PK", "Packaging"
        )
        assert "error" not in mcp_server.update_site(
            ids["north"], description="Main brewery"
        )
        assert "error" in mcp_server.create_area(
            ids["south"], "PK", "Packaging"
        )
        # Company-wide changes need an admin of every site.
        refused = mcp_server.create_lookup(ids["atlas"], "Brand")
        assert "every site" in refused["error"]
        assert "every site" in mcp_server.create_site(
            ids["atlas"], "WST", "West"
        )["error"]
        assert "every site" in mcp_server.delete_site(ids["north"])["error"]


def test_an_all_sites_admin_configures_the_company(world):
    ids, as_ = world
    with as_("atlas_admin"):
        assert "error" not in mcp_server.create_lookup(ids["atlas"], "Brand")
        assert "error" not in mcp_server.create_site(
            ids["atlas"], "WST", "West", timezone="UTC"
        )
        assert "error" in mcp_server.create_lookup(ids["borealis"], "Brand")


def test_only_superusers_create_or_delete_companies(world):
    ids, as_ = world
    with as_("atlas_admin"):
        assert "superuser" in mcp_server.create_enterprise("NEW", "New")[
            "error"
        ]
        assert "superuser" in mcp_server.delete_enterprise(ids["atlas"])[
            "error"
        ]


# -- people ---------------------------------------------------------------

def test_admins_see_only_their_companys_people(world):
    ids, as_ = world
    with as_("atlas_admin"):
        emails = {u["email"] for u in mcp_server.list_users()}
        assert emails == {
            "north_operator@brewery.com",
            "north_admin@brewery.com",
            "atlas_admin@brewery.com",
        }
        assert {m["enterprise_id"] for m in mcp_server.list_memberships()} == {
            ids["atlas"]
        }


def test_an_admin_onboards_someone(world):
    ids, as_ = world
    with as_("atlas_admin"):
        created = mcp_server.create_user("new@brewery.com", name="New")
        assert "no access yet" in created["message"]
        granted = mcp_server.create_membership(
            "new@brewery.com", ids["atlas"], "operator", [ids["south"]]
        )
        assert granted["site_ids"] == [ids["south"]]
        assert "error" in mcp_server.create_membership(
            "new@brewery.com", ids["borealis"], "operator"
        )
        assert "error" in mcp_server.create_user(
            "boss@brewery.com", is_superuser=True
        )


def test_a_site_admin_cannot_manage_people(world):
    ids, as_ = world
    with as_("north_admin"):
        assert "error" in mcp_server.create_user("new@brewery.com")
        assert [u["email"] for u in mcp_server.list_users()] == [
            "north_admin@brewery.com"  # just themselves
        ]
        assert "error" in mcp_server.create_membership(
            "north_admin@brewery.com", ids["atlas"], "admin"
        )


def test_people_can_change_only_their_own_name_and_timezone(world):
    _, as_ = world
    with as_("north_admin"):
        (me,) = mcp_server.list_users()
        mine = mcp_server.update_user(me["id"], timezone="America/Denver")
        assert mine["timezone"] == "America/Denver"
        assert "superuser" in mcp_server.update_user(
            me["id"], is_superuser=True
        )["error"]
    with as_("atlas_admin"):
        assert "superuser" in mcp_server.update_user(
            me["id"], name="Renamed"
        )["error"]
        assert "superuser" in mcp_server.update_user(
            me["id"], is_active=False
        )["error"]


def test_membership_changes_take_effect_on_the_next_call(world):
    ids, as_ = world
    with as_("atlas_admin"):
        membership = next(
            m
            for m in mcp_server.list_memberships()
            if m["email"] == "north_operator@brewery.com"
        )
        mcp_server.update_membership(membership["id"], has_all_sites=True)
    with as_("north_operator"):
        assert "error" not in mcp_server.get_element(ids["south_fv"])


# -- every tool decides its access ----------------------------------------

def _tool_names(server):
    async def names():
        async with Client(server) as client:
            return {t.name: t for t in await client.list_tools()}

    return asyncio.run(names())


def test_every_tool_is_guarded():
    """A tool with no @_guard is refused here, so none ships unscoped."""
    admin = FastMCP("admin")
    mcp_server._register_config_tools(admin)
    names = set(_tool_names(mcp_server.mcp)) | set(_tool_names(admin))
    unguarded = sorted(
        n for n in names if not getattr(getattr(mcp_server, n), "guarded", 0)
    )
    assert unguarded == []


def test_guarding_keeps_the_tool_schemas():
    admin = FastMCP("admin")
    mcp_server._register_config_tools(admin)
    schema = _tool_names(admin)["create_membership"].inputSchema
    assert set(schema["properties"]) == {
        "email",
        "enterprise_id",
        "role",
        "site_ids",
    }
    assert set(schema["required"]) == {"email", "enterprise_id", "role"}
