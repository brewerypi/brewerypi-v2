"""Tests for the current-user context and the tier gate."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.identity import (
    CurrentUser,
    authorize_tier,
    current_user,
    signed_in_as,
)
from brewerypi.models import Enterprise
from brewerypi.services import (
    PermissionDeniedError,
    create_membership,
    create_user,
)


@pytest.fixture
def ctx():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E1", name="Ent One")
        other = Enterprise(abbreviation="E2", name="Ent Two")
        session.add_all([ent, other])
        session.flush()
        yield session, ent.id, other.id


def test_nobody_is_signed_in_by_default():
    assert current_user() is None


def test_signed_in_as_sets_and_restores():
    sam = CurrentUser(id=1, email="sam@x.com", timezone=None,
                      is_superuser=False)
    with signed_in_as(sam):
        assert current_user() == sam
    assert current_user() is None


def test_signed_in_as_restores_after_an_error():
    sam = CurrentUser(id=1, email="sam@x.com", timezone=None,
                      is_superuser=False)
    with pytest.raises(RuntimeError):
        with signed_in_as(sam):
            raise RuntimeError
    assert current_user() is None


def test_snapshot_copies_the_user(ctx):
    session, _, _ = ctx
    user = create_user(
        session, "sam@x.com", timezone="America/Denver", is_superuser=True
    )
    assert CurrentUser.from_user(user) == CurrentUser(
        id=user.id,
        email="sam@x.com",
        timezone="America/Denver",
        is_superuser=True,
    )


def test_no_membership_is_refused_on_both_tiers(ctx):
    session, _, _ = ctx
    user = create_user(session, "sam@x.com")
    for tier in ("operator", "admin"):
        with pytest.raises(PermissionDeniedError, match="access"):
            authorize_tier(user, tier)


def test_operator_membership_opens_only_the_operator_tier(ctx):
    session, ent, _ = ctx
    user = create_user(session, "sam@x.com")
    create_membership(session, user.id, ent, "operator")
    session.refresh(user)
    authorize_tier(user, "operator")
    with pytest.raises(PermissionDeniedError, match="operator connector"):
        authorize_tier(user, "admin")


def test_admin_anywhere_opens_the_admin_tier(ctx):
    session, ent, other = ctx
    user = create_user(session, "sam@x.com")
    create_membership(session, user.id, ent, "operator")
    create_membership(session, user.id, other, "admin")
    session.refresh(user)
    authorize_tier(user, "operator")
    authorize_tier(user, "admin")


def test_superuser_needs_no_membership(ctx):
    session, _, _ = ctx
    user = create_user(session, "root@x.com", is_superuser=True)
    authorize_tier(user, "operator")
    authorize_tier(user, "admin")


def test_inactive_is_refused_even_for_a_superuser(ctx):
    session, ent, _ = ctx
    user = create_user(session, "root@x.com", is_superuser=True)
    create_membership(session, user.id, ent, "admin")
    user.is_active = False
    for tier in ("operator", "admin"):
        with pytest.raises(PermissionDeniedError, match="deactivated"):
            authorize_tier(user, tier)


def test_unknown_tier_is_a_programming_error(ctx):
    session, _, _ = ctx
    user = create_user(session, "root@x.com", is_superuser=True)
    with pytest.raises(ValueError):
        authorize_tier(user, "Admin")
