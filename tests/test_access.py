"""Tests for per-enterprise / per-site access decisions."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.access import (
    CONFIGURE,
    CONFIGURE_ENTERPRISE,
    OPERATE,
    READ,
    SUPERUSER,
    Grant,
    Place,
    allowed,
    check,
    locate,
    visible,
)
from brewerypi.database import Base
from brewerypi.identity import CurrentUser
from brewerypi.models import Area, Enterprise, Lookup, Site, Tag
from brewerypi.services import (
    NotFoundError,
    PermissionDeniedError,
    create_element,
    create_element_template,
    create_event_frame,
    create_event_frame_template,
    create_membership,
    create_user,
)


def _user(*grants, superuser=False):
    return CurrentUser(
        id=1,
        email="u@brewery.com",
        timezone=None,
        is_superuser=superuser,
        grants=tuple(grants),
    )


A, B = 1, 2
A_SITE, A_OTHER_SITE = 10, 11


@pytest.mark.parametrize(
    "grant, place, level, expected",
    [
        # Any member reads the company-wide rows and their own sites.
        (Grant(A, "operator", None), Place(A), READ, True),
        (Grant(A, "operator", frozenset({A_SITE})), Place(A), READ, True),
        (Grant(A, "operator", None), Place(A, A_SITE), OPERATE, True),
        # A site restriction hides the other sites.
        (
            Grant(A, "operator", frozenset({A_SITE})),
            Place(A, A_OTHER_SITE),
            READ,
            False,
        ),
        # Another company is invisible whatever the role.
        (Grant(A, "admin", None), Place(B), READ, False),
        # Operators never configure.
        (Grant(A, "operator", None), Place(A, A_SITE), CONFIGURE, False),
        # A site admin configures their site, not the company's rows.
        (
            Grant(A, "admin", frozenset({A_SITE})),
            Place(A, A_SITE),
            CONFIGURE,
            True,
        ),
        (Grant(A, "admin", frozenset({A_SITE})), Place(A), CONFIGURE, False),
        (
            Grant(A, "admin", frozenset({A_SITE})),
            Place(A, A_SITE),
            CONFIGURE_ENTERPRISE,
            False,
        ),
        # An all-sites admin does both.
        (Grant(A, "admin", None), Place(A), CONFIGURE, True),
        (
            Grant(A, "admin", None),
            Place(A, A_SITE),
            CONFIGURE_ENTERPRISE,
            True,
        ),
        # Only superusers act above companies.
        (Grant(A, "admin", None), Place(A), SUPERUSER, False),
        # An emptied site list fails closed: company-wide reads only.
        (Grant(A, "operator", frozenset()), Place(A, A_SITE), READ, False),
        (Grant(A, "operator", frozenset()), Place(A), READ, True),
    ],
)
def test_allowed(grant, place, level, expected):
    assert allowed(_user(grant), place, level) is expected


def test_nobody_signed_in_and_superusers_are_unrestricted():
    for user in (None, _user(superuser=True)):
        assert allowed(user, Place(B, 99), SUPERUSER)


def test_unknown_level_is_a_bug():
    with pytest.raises(ValueError):
        allowed(None, Place(A), "everything")


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _brewery(session, abbreviation):
    ent = Enterprise(abbreviation=abbreviation, name=f"Co {abbreviation}")
    site = Site(abbreviation="S", name="Site", timezone="UTC")
    area = Area(abbreviation="A", name="Area")
    site.areas.append(area)
    ent.sites.append(site)
    ent.lookups.append(Lookup(name="Status"))
    session.add(ent)
    session.flush()
    tag = Tag(name="T", area_id=area.id)
    session.add(tag)
    template = create_element_template(session, site.id, "Fermenter")
    element = create_element(session, template.id, "FV01", tag_area_id=area.id)
    batch_type = create_event_frame_template(
        session, template.id, "Fermentation"
    )
    frame = create_event_frame(session, element.id, batch_type.id, "B1")
    return ent, site, area, tag, element, frame


def test_locate_walks_up_to_the_site(session):
    ent, site, area, tag, element, frame = _brewery(session, "A")
    for row in (site, area, tag, element, frame):
        assert locate(session, row) == Place(ent.id, site.id)
    assert locate(session, ent) == Place(ent.id)
    assert locate(session, ent.lookups[0]) == Place(ent.id)


def test_locate_refuses_an_unknown_kind_of_row(session):
    with pytest.raises(TypeError):
        locate(session, object())


def test_check_hides_other_companies_and_explains_refusals(session):
    ent, site, *_, frame = _brewery(session, "A")
    other = _brewery(session, "B")[-1]
    operator = _user(Grant(ent.id, "operator", None))

    with pytest.raises(NotFoundError, match="no event frame with id"):
        check(session, operator, other, READ)
    check(session, operator, frame, OPERATE)
    with pytest.raises(PermissionDeniedError, match="not an administrator"):
        check(session, operator, site, CONFIGURE)


def test_visible_filters_to_what_the_user_may_read(session):
    a = _brewery(session, "A")
    b = _brewery(session, "B")
    operator = _user(Grant(a[0].id, "operator", None))
    assert visible(session, operator, [a[3], b[3]]) == [a[3]]
    assert visible(session, None, [a[3], b[3]]) == [a[3], b[3]]


def test_grants_snapshot_memberships(session):
    ent, site, *_ = _brewery(session, "A")
    user = create_user(session, "brewer@brewery.com")
    create_membership(session, user.id, ent.id, "operator", [site.id])
    snapshot = CurrentUser.from_user(user)
    assert snapshot.grants == (
        Grant(ent.id, "operator", frozenset({site.id})),
    )
