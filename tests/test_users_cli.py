"""Tests for the ``brewerypi-users`` bootstrap CLI."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import users_cli
from brewerypi.database import Base
from brewerypi.models import Enterprise, Site
from brewerypi.services import (
    NotFoundError,
    find_membership,
    get_user_by_email,
    membership_site_ids,
)


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cli.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="BRW", name="Brewery Co")
        ent.sites.extend(
            [
                Site(abbreviation="ATL", name="Atlanta"),
                Site(abbreviation="DEN", name="Denver"),
            ]
        )
        session.add(ent)
        session.commit()
    return sessionmaker(engine)


@pytest.fixture
def run(factory, capsys):
    """Run a CLI command line; return (status, stdout, stderr)."""

    def _run(*argv):
        status = users_cli.main(list(argv), session_factory=factory)
        out = capsys.readouterr()
        return status, out.out, out.err

    return _run


def _user(factory, email):
    with factory() as session:
        user = get_user_by_email(session, email)
        session.expunge(user)
        return user


def test_bootstrap_a_superuser(run, factory):
    status, out, _ = run(
        "add", "Root@Brewery.com", "--name", "Root", "--superuser"
    )
    assert status == 0
    assert "root@brewery.com (Root) [superuser, not signed in yet]" in out
    assert _user(factory, "root@brewery.com").is_superuser


def test_add_reminds_that_a_plain_user_has_no_access(run):
    _, out, _ = run("add", "brewer@brewery.com")
    assert "no access yet" in out


def test_errors_exit_nonzero_and_change_nothing(run, factory):
    run("add", "brewer@brewery.com")
    status, _, err = run("add", "BREWER@brewery.com")
    assert status == 1
    assert "already exists" in err

    status, _, err = run(
        "add", "zone@brewery.com", "--timezone", "Mars/Base"
    )
    assert status == 1
    with factory() as session:
        with pytest.raises(NotFoundError):
            get_user_by_email(session, "zone@brewery.com")


def test_grant_all_sites_then_restrict_then_reopen(run, factory):
    run("add", "brewer@brewery.com")
    status, out, _ = run("grant", "brewer@brewery.com", "BRW",
                         "--role", "operator")
    assert status == 0
    assert "operator at Brewery Co (all sites)" in out

    _, out, _ = run("grant", "brewer@brewery.com", "BRW", "--role", "admin",
                    "--site", "DEN", "--site", "ATL")
    assert "admin at Brewery Co (ATL, DEN)" in out

    _, out, _ = run("grant", "brewer@brewery.com", "1", "--role", "admin")
    assert "(all sites)" in out
    with factory() as session:
        user = get_user_by_email(session, "brewer@brewery.com")
        membership = find_membership(session, user.id, 1)
        assert membership.role == "admin"
        assert membership_site_ids(membership) is None


def test_grant_rejects_unknown_enterprise_or_site(run):
    run("add", "brewer@brewery.com")
    status, _, err = run("grant", "brewer@brewery.com", "NOPE",
                         "--role", "operator")
    assert status == 1 and "NOPE" in err
    status, _, err = run("grant", "brewer@brewery.com", "BRW",
                         "--role", "operator", "--site", "LAX")
    assert status == 1 and "LAX" in err


def test_revoke(run, factory):
    run("add", "brewer@brewery.com")
    run("grant", "brewer@brewery.com", "BRW", "--role", "operator")
    status, out, _ = run("revoke", "brewer@brewery.com", "BRW")
    assert status == 0 and "removed from Brewery Co" in out
    status, _, err = run("revoke", "brewer@brewery.com", "BRW")
    assert status == 1 and "no membership" in err


def test_update_deactivate_and_timezone(run, factory):
    run("add", "brewer@brewery.com", "--timezone", "UTC")
    status, out, _ = run("update", "brewer@brewery.com", "--no-active",
                         "--timezone", "")
    assert status == 0 and "INACTIVE" in out
    user = _user(factory, "brewer@brewery.com")
    assert user.is_active is False
    assert user.timezone is None


def test_reset_sign_in(run, factory):
    run("add", "brewer@brewery.com")
    with factory() as session:
        get_user_by_email(session, "brewer@brewery.com").auth_subject = "u1"
        session.commit()
    status, _, _ = run("reset-sign-in", "brewer@brewery.com")
    assert status == 0
    assert _user(factory, "brewer@brewery.com").auth_subject is None


def test_remove_needs_yes(run, factory):
    run("add", "brewer@brewery.com")
    status, _, err = run("remove", "brewer@brewery.com")
    assert status == 1 and "--no-active" in err
    status, _, _ = run("remove", "brewer@brewery.com", "--yes")
    assert status == 0
    _, out, _ = run("list")
    assert "no users" in out


def test_list_shows_memberships(run):
    run("add", "brewer@brewery.com", "--timezone", "America/Denver")
    run("grant", "brewer@brewery.com", "BRW", "--role", "operator",
        "--site", "ATL")
    _, out, _ = run("list")
    assert "brewer@brewery.com [not signed in yet]  timezone " \
        "America/Denver" in out
    assert "  operator at Brewery Co (ATL)" in out
    _, show, _ = run("show", "brewer@brewery.com")
    assert show == out
