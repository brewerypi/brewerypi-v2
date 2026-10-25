"""Tests for the membership service functions and site restrictions."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Enterprise, Site
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_membership,
    create_user,
    delete_membership,
    find_membership,
    get_membership,
    list_memberships,
    membership_site_ids,
    update_membership,
)


@pytest.fixture
def ctx():
    """Two enterprises (the first with two sites) and one user."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E1", name="Ent One")
        ent.sites.extend(
            [
                Site(abbreviation="ATL", name="Atlanta"),
                Site(abbreviation="DEN", name="Denver"),
            ]
        )
        other = Enterprise(abbreviation="E2", name="Ent Two")
        other.sites.append(Site(abbreviation="X", name="Elsewhere"))
        session.add_all([ent, other])
        user = create_user(session, "brewer@example.com")
        session.flush()
        yield session, {
            "ent": ent.id,
            "other": other.id,
            "atl": ent.sites[0].id,
            "den": ent.sites[1].id,
            "x": other.sites[0].id,
            "user": user.id,
        }


def test_create_covers_all_sites_by_default(ctx):
    session, ids = ctx
    m = create_membership(session, ids["user"], ids["ent"], "Operator")
    assert m.role == "operator"
    assert m.has_all_sites is True
    assert membership_site_ids(m) is None
    assert get_membership(session, m.id) is m
    assert find_membership(session, ids["user"], ids["ent"]) is m


def test_create_restricted_to_sites(ctx):
    session, ids = ctx
    m = create_membership(
        session, ids["user"], ids["ent"], "admin",
        site_ids=[ids["den"], ids["atl"], ids["den"]],
    )
    assert m.has_all_sites is False
    assert membership_site_ids(m) == sorted([ids["atl"], ids["den"]])


def test_rejects_unknown_role(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError, match="role"):
        create_membership(session, ids["user"], ids["ent"], "owner")


def test_rejects_missing_user_or_enterprise(ctx):
    session, ids = ctx
    with pytest.raises(NotFoundError):
        create_membership(session, 999, ids["ent"], "operator")
    with pytest.raises(NotFoundError):
        create_membership(session, ids["user"], 999, "operator")


def test_one_membership_per_user_and_enterprise(ctx):
    session, ids = ctx
    create_membership(session, ids["user"], ids["ent"], "operator")
    with pytest.raises(ConflictError):
        create_membership(session, ids["user"], ids["ent"], "admin")
    # a second enterprise is fine
    create_membership(session, ids["user"], ids["other"], "admin")
    assert len(list_memberships(session, user_id=ids["user"])) == 2
    assert len(list_memberships(session, enterprise_id=ids["other"])) == 1


def test_sites_must_exist_and_belong_to_the_enterprise(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError, match="different enterprise"):
        create_membership(
            session, ids["user"], ids["ent"], "operator", site_ids=[ids["x"]]
        )
    with pytest.raises(NotFoundError):
        create_membership(
            session, ids["user"], ids["ent"], "operator", site_ids=[999]
        )
    # a refused create leaves nothing half-made behind
    assert find_membership(session, ids["user"], ids["ent"]) is None


def test_update_replaces_lifts_and_restricts(ctx):
    session, ids = ctx
    m = create_membership(
        session, ids["user"], ids["ent"], "operator", site_ids=[ids["atl"]]
    )
    update_membership(session, m.id, site_ids=[ids["den"]])
    assert membership_site_ids(m) == [ids["den"]]

    update_membership(session, m.id, role="admin", has_all_sites=True)
    assert m.role == "admin"
    assert membership_site_ids(m) is None

    # restricting with no list means no sites, never all of them
    update_membership(session, m.id, has_all_sites=False)
    assert membership_site_ids(m) == []

    update_membership(session, m.id, site_ids=[])
    assert membership_site_ids(m) == []


def test_update_refuses_all_sites_with_a_list(ctx):
    session, ids = ctx
    m = create_membership(session, ids["user"], ids["ent"], "operator")
    with pytest.raises(ValidationError):
        update_membership(
            session, m.id, has_all_sites=True, site_ids=[ids["atl"]]
        )


def test_delete(ctx):
    session, ids = ctx
    m = create_membership(
        session, ids["user"], ids["ent"], "operator", site_ids=[ids["atl"]]
    )
    delete_membership(session, m.id)
    assert find_membership(session, ids["user"], ids["ent"]) is None
    with pytest.raises(NotFoundError):
        get_membership(session, m.id)
