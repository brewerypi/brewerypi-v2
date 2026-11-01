"""Tests for the user service functions, including sign-in linking."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site, Tag, TagValue
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
    attributed_counts,
    create_user,
    delete_user,
    get_user,
    get_user_by_email,
    get_user_by_subject,
    link_sign_in,
    list_users,
    reset_sign_in,
    update_user,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_delete_is_refused_for_someone_a_reading_names(session):
    author = create_user(session, "author@example.com")
    corrector = create_user(session, "corrector@example.com")
    bystander = create_user(session, "bystander@example.com")
    site = Site(abbreviation="S", name="Site", timezone="UTC")
    site.areas.append(Area(abbreviation="A", name="Area"))
    session.add(Enterprise(abbreviation="E", name="Co", sites=[site]))
    session.flush()
    tag = Tag(name="T", area_id=site.areas[0].id)
    session.add(tag)
    session.flush()
    session.add(
        TagValue(
            tag_id=tag.id,
            observed_at=datetime(2026, 1, 1),
            value=1.0,
            recorded_by_id=author.id,
            updated_by_id=corrector.id,
        )
    )
    session.flush()

    assert attributed_counts(session, author.id) == (1, 0)
    for person in (author, corrector):
        with pytest.raises(ConflictError, match="deactivate"):
            delete_user(session, person.id)
    delete_user(session, bystander.id)
    assert attributed_counts(session, bystander.id) == (0, 0)


def test_create_normalizes_email_and_defaults(session):
    user = create_user(session, "  Brewer@Example.COM ", name=" Sam ")
    assert user.email == "brewer@example.com"
    assert user.name == "Sam"
    assert user.timezone is None
    assert user.is_superuser is False
    assert user.is_active is True
    assert get_user(session, user.id) is user


def test_email_is_unique_regardless_of_case(session):
    create_user(session, "brewer@example.com")
    with pytest.raises(ConflictError):
        create_user(session, "BREWER@example.com")


@pytest.mark.parametrize(
    "email", ["", "brewer", "@example.com", "brewer@localhost", "a b@x.com"]
)
def test_rejects_non_emails(session, email):
    with pytest.raises(ValidationError):
        create_user(session, email)


def test_timezone_is_validated(session):
    with pytest.raises(ValidationError):
        create_user(session, "brewer@example.com", timezone="Mars/Base")
    user = create_user(
        session, "brewer@example.com", timezone="America/Denver"
    )
    assert user.timezone == "America/Denver"


def test_get_by_email_any_case(session):
    user = create_user(session, "brewer@example.com")
    assert get_user_by_email(session, "Brewer@Example.com") is user
    with pytest.raises(NotFoundError):
        get_user_by_email(session, "nobody@example.com")


def test_list_orders_by_email(session):
    create_user(session, "zed@example.com")
    create_user(session, "amy@example.com")
    assert [u.email for u in list_users(session)] == [
        "amy@example.com",
        "zed@example.com",
    ]


def test_update_only_changes_given_fields_and_blank_clears(session):
    user = create_user(
        session, "brewer@example.com", name="Sam", timezone="UTC"
    )
    update_user(session, user.id, is_superuser=True)
    assert (user.name, user.timezone, user.is_superuser) == (
        "Sam",
        "UTC",
        True,
    )
    update_user(session, user.id, name="", timezone="", is_active=False)
    assert user.name is None
    assert user.timezone is None
    assert user.is_active is False


def test_delete(session):
    user = create_user(session, "brewer@example.com")
    delete_user(session, user.id)
    with pytest.raises(NotFoundError):
        get_user(session, user.id)


# -- sign-in linking -------------------------------------------------------

def test_first_sign_in_links_by_verified_email(session):
    user = create_user(session, "brewer@example.com")
    linked = link_sign_in(session, "user_01", "Brewer@example.com", True)
    assert linked is user
    assert user.auth_subject == "user_01"
    assert get_user_by_subject(session, "user_01") is user


def test_linked_subject_is_found_without_email(session):
    user = create_user(session, "brewer@example.com")
    link_sign_in(session, "user_01", "brewer@example.com", True)
    assert link_sign_in(session, "user_01", None, False) is user


def test_unverified_or_missing_email_is_refused(session):
    create_user(session, "brewer@example.com")
    with pytest.raises(PermissionDeniedError, match="verified"):
        link_sign_in(session, "user_01", "brewer@example.com", False)
    with pytest.raises(PermissionDeniedError, match="verified"):
        link_sign_in(session, "user_01", None, True)


def test_email_not_on_the_list_is_refused(session):
    with pytest.raises(PermissionDeniedError, match="not a Brewery Pi user"):
        link_sign_in(session, "user_01", "stranger@example.com", True)


def test_a_second_subject_for_a_linked_email_is_refused(session):
    """A recreated account needs an admin's reset, not a silent relink."""
    user = create_user(session, "brewer@example.com")
    link_sign_in(session, "user_01", "brewer@example.com", True)
    with pytest.raises(PermissionDeniedError, match="different sign-in"):
        link_sign_in(session, "user_02", "brewer@example.com", True)

    reset_sign_in(session, user.id)
    assert user.auth_subject is None
    assert link_sign_in(session, "user_02", "brewer@example.com", True) is (
        user
    )
    assert user.auth_subject == "user_02"
