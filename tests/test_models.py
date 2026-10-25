"""Tests for all domain models — verifies relationships, navigation, and
cascade deletes. Uses an in-memory SQLite database, so no setup or cleanup
is required.
"""

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Element,
    ElementTemplate,
    Enterprise,
    EventFrame,
    EventFrameTemplate,
    Lookup,
    LookupValue,
    MeasurementUnit,
    Membership,
    MembershipSite,
    Site,
    Tag,
    TagValue,
    User,
)


def make_engine():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def test_hierarchy_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        site = Site(abbreviation="HQ", name="Headquarters")
        site.areas.append(Area(abbreviation="MASH", name="Mash House"))
        enterprise.sites.append(site)
        session.add(enterprise)
        session.commit()

        loaded = session.scalar(select(Enterprise))
        assert loaded is not None
        assert [s.name for s in loaded.sites] == ["Headquarters"]
        assert loaded.sites[0].areas[0].name == "Mash House"

        # navigate back up the hierarchy
        area = session.scalar(select(Area))
        assert area.site.enterprise.name == "Brewery Co"

        # deleting the enterprise cascades to its sites and areas
        session.delete(loaded)
        session.commit()
        assert session.scalar(select(func.count()).select_from(Site)) == 0
        assert session.scalar(select(func.count()).select_from(Area)) == 0


def test_lookup_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        lookup = Lookup(name="Fermentation Stage")
        lookup.lookup_values.append(
            LookupValue(name="Primary", is_selectable=True)
        )
        lookup.lookup_values.append(
            LookupValue(name="Secondary", is_selectable=True)
        )
        lookup.lookup_values.append(
            LookupValue(name="Conditioning", is_selectable=False)
        )
        enterprise.lookups.append(lookup)
        session.add(enterprise)
        session.commit()

        # navigate enterprise -> lookup -> lookup_values
        loaded = session.scalar(select(Enterprise))
        assert loaded is not None
        assert [lk.name for lk in loaded.lookups] == ["Fermentation Stage"]
        values = loaded.lookups[0].lookup_values
        assert sorted(v.name for v in values) == [
            "Conditioning",
            "Primary",
            "Secondary",
        ]

        # navigate lookup_value -> lookup -> enterprise
        lv = session.scalar(
            select(LookupValue).where(LookupValue.name == "Primary")
        )
        assert lv.lookup.name == "Fermentation Stage"
        assert lv.lookup.enterprise.name == "Brewery Co"

        # verify is_selectable is stored correctly
        conditioning = session.scalar(
            select(LookupValue).where(LookupValue.name == "Conditioning")
        )
        assert conditioning.is_selectable is False

        # deleting the enterprise cascades to its lookups and lookup_values
        session.delete(loaded)
        session.commit()
        assert (
            session.scalar(select(func.count()).select_from(Lookup)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(LookupValue)) == 0
        )


def test_measurement_unit_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        enterprise.measurement_units.append(
            MeasurementUnit(abbreviation="°C", name="Celsius")
        )
        enterprise.measurement_units.append(
            MeasurementUnit(abbreviation="°P", name="Plato")
        )
        session.add(enterprise)
        session.commit()

        loaded = session.scalar(select(Enterprise))
        names = sorted(mu.name for mu in loaded.measurement_units)
        assert names == ["Celsius", "Plato"]

        mu = session.scalar(
            select(MeasurementUnit).where(MeasurementUnit.name == "Celsius")
        )
        assert mu.enterprise.name == "Brewery Co"
        assert mu.abbreviation == "°C"

        # deleting the enterprise cascades to its measurement_units
        session.delete(loaded)
        session.commit()
        assert (
            session.scalar(
                select(func.count()).select_from(MeasurementUnit)
            ) == 0
        )


def test_numeric_tag_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        mu = MeasurementUnit(abbreviation="°C", name="Celsius")
        enterprise.measurement_units.append(mu)
        site = Site(abbreviation="HQ", name="Headquarters")
        area = Area(abbreviation="BH", name="Brewhouse")
        site.areas.append(area)
        enterprise.sites.append(site)
        session.add(enterprise)
        session.flush()

        tag = Tag(name="Mash Temperature", measurement_unit_id=mu.id)
        area.tags.append(tag)
        session.flush()

        ts = __import__("datetime").datetime(2024, 1, 1, 12, 0, 0)
        tag.tag_values.append(TagValue(observed_at=ts, value=68.5))
        session.commit()

        loaded_tag = session.scalar(select(Tag))
        assert loaded_tag.name == "Mash Temperature"
        assert loaded_tag.lookup is None
        assert loaded_tag.measurement_unit.name == "Celsius"
        assert loaded_tag.area.name == "Brewhouse"

        tv = loaded_tag.tag_values[0]
        assert tv.value == 68.5
        assert tv.lookup_value is None

        # deleting the area cascades to its tags and tag_values
        session.delete(loaded_tag.area)
        session.commit()
        assert session.scalar(select(func.count()).select_from(Tag)) == 0
        assert (
            session.scalar(select(func.count()).select_from(TagValue)) == 0
        )


def test_lookup_tag_navigation_and_cascade():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        lookup = Lookup(name="Fermentation Stage")
        lv_primary = LookupValue(name="Primary", is_selectable=True)
        lookup.lookup_values.append(lv_primary)
        enterprise.lookups.append(lookup)
        site = Site(abbreviation="HQ", name="Headquarters")
        area = Area(abbreviation="BH", name="Brewhouse")
        site.areas.append(area)
        enterprise.sites.append(site)
        session.add(enterprise)
        session.flush()

        tag = Tag(name="Fermentation Stage Tag", lookup_id=lookup.id)
        area.tags.append(tag)
        session.flush()

        ts = __import__("datetime").datetime(2024, 1, 1, 12, 0, 0)
        tag.tag_values.append(
            TagValue(observed_at=ts, lookup_value_id=lv_primary.id)
        )
        session.commit()

        loaded_tag = session.scalar(select(Tag))
        assert loaded_tag.lookup.name == "Fermentation Stage"
        assert loaded_tag.measurement_unit is None

        tv = loaded_tag.tag_values[0]
        assert tv.value is None
        assert tv.lookup_value.name == "Primary"

        # deleting the tag cascades to its tag_values
        session.delete(loaded_tag)
        session.commit()
        assert (
            session.scalar(select(func.count()).select_from(TagValue)) == 0
        )
        # lookup_value still exists — tag_values was the only reference
        assert (
            session.scalar(
                select(func.count()).select_from(LookupValue)
            ) == 1
        )


def test_tag_value_requires_exactly_one_value_column():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        site = Site(abbreviation="HQ", name="Headquarters")
        area = Area(abbreviation="BH", name="Brewhouse")
        site.areas.append(area)
        enterprise.sites.append(site)
        session.add(enterprise)
        session.flush()

        tag = Tag(name="Mash Temp")
        area.tags.append(tag)
        session.flush()

        ts = __import__("datetime").datetime(2024, 1, 1, 12, 0, 0)

        # both null — must be rejected
        session.add(TagValue(tag_id=tag.id, observed_at=ts))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        # both non-null — must be rejected
        lookup = Lookup(name="Stage")
        lv = LookupValue(name="Primary", is_selectable=True)
        lookup.lookup_values.append(lv)
        enterprise.lookups.append(lookup)
        session.flush()

        session.add(
            TagValue(
                tag_id=tag.id,
                observed_at=ts,
                value=68.5,
                lookup_value_id=lv.id,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_lookup_value_delete_blocked_when_referenced_by_tag_value():
    engine = make_engine()
    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        lookup = Lookup(name="Fermentation Stage")
        lv = LookupValue(name="Primary", is_selectable=True)
        lookup.lookup_values.append(lv)
        enterprise.lookups.append(lookup)
        site = Site(abbreviation="HQ", name="Headquarters")
        area = Area(abbreviation="BH", name="Brewhouse")
        site.areas.append(area)
        enterprise.sites.append(site)
        session.add(enterprise)
        session.flush()

        tag = Tag(name="Stage Tag", lookup_id=lookup.id)
        area.tags.append(tag)
        session.flush()

        ts = __import__("datetime").datetime(2024, 1, 1, 12, 0, 0)
        tag.tag_values.append(TagValue(observed_at=ts, lookup_value_id=lv.id))
        session.commit()

    # attempt to delete the LookupValue in a fresh session
    with Session(engine) as session:
        lv = session.scalar(select(LookupValue))
        session.delete(lv)
        with pytest.raises(IntegrityError):
            session.commit()


def make_transfer(session):
    """A knockout frame linking a brew to a fermentation. Returns its id."""
    import datetime

    enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
    site = Site(abbreviation="HQ", name="Headquarters")
    enterprise.sites.append(site)
    session.add(enterprise)
    session.flush()
    template = ElementTemplate(site_id=site.id, name="Brewhouse")
    session.add(template)
    session.flush()
    element = Element(element_template_id=template.id, name="BH1")
    session.add(element)
    frame_template = EventFrameTemplate(
        element_template_id=template.id, name="Brew", step_order=1
    )
    session.add(frame_template)
    session.flush()

    def frame(name, hour, **links):
        f = EventFrame(
            element_id=element.id,
            event_frame_template_id=frame_template.id,
            name=name,
            started_at=datetime.datetime(2026, 7, 17, hour),
            **links,
        )
        session.add(f)
        session.flush()
        return f

    brew = frame("Brew #1", 6)
    ferm = frame("FV01 B123", 5)
    transfer = frame(
        "Knockout", 8,
        source_event_frame_id=brew.id,
        destination_event_frame_id=ferm.id,
    )
    session.commit()
    return transfer.id


def test_transfer_links_navigate_and_stay_separate_from_nesting():
    engine = make_engine()
    with Session(engine) as session:
        transfer_id = make_transfer(session)
    with Session(engine) as session:
        transfer = session.get(EventFrame, transfer_id)
        assert transfer.source.name == "Brew #1"
        assert transfer.destination.name == "FV01 B123"
        # flow is not containment: three self-FKs, and only one of them nests
        assert transfer.parent is None
        assert transfer.source.children == []


def test_event_frame_delete_blocked_while_a_transfer_points_at_it():
    """The DB-level backstop behind the service's delete guard."""
    engine = make_engine()
    with Session(engine) as session:
        transfer_id = make_transfer(session)
    with Session(engine) as session:
        transfer = session.get(EventFrame, transfer_id)
        session.delete(transfer.source)
        with pytest.raises(IntegrityError):
            session.commit()


def test_event_frame_template_parent_flow_is_optional_and_persists():
    engine = make_engine()
    with Session(engine) as session:
        make_transfer(session)
        brew = session.scalars(select(EventFrameTemplate)).one()
        assert brew.parent_flow is None
        cooling = EventFrameTemplate(
            element_template_id=brew.element_template_id,
            parent_id=brew.id,
            name="Wort cooling",
            step_order=1,
            parent_flow="source",
        )
        session.add(cooling)
        session.commit()
        cooling_id = cooling.id
    with Session(engine) as session:
        cooling = session.get(EventFrameTemplate, cooling_id)
        assert cooling.parent_flow == "source"


def _member_ctx(session):
    enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
    site = Site(abbreviation="HQ", name="Headquarters")
    enterprise.sites.append(site)
    user = User(email="brewer@example.com")
    session.add_all([enterprise, user])
    session.flush()
    return enterprise, site, user


def test_membership_role_must_be_known():
    engine = make_engine()
    with Session(engine) as session:
        enterprise, _, user = _member_ctx(session)
        session.add(
            Membership(
                user_id=user.id, enterprise_id=enterprise.id, role="owner"
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_one_membership_per_user_and_enterprise():
    engine = make_engine()
    with Session(engine) as session:
        enterprise, _, user = _member_ctx(session)
        for role in ("operator", "admin"):
            session.add(
                Membership(
                    user_id=user.id, enterprise_id=enterprise.id, role=role
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()


def test_user_defaults_and_unique_email():
    engine = make_engine()
    with Session(engine) as session:
        _, _, user = _member_ctx(session)
        assert user.is_active is True
        assert user.is_superuser is False
        assert user.auth_subject is None
        session.add(User(email="brewer@example.com"))
        with pytest.raises(IntegrityError):
            session.flush()


def test_site_delete_leaves_a_restricted_membership_closed():
    """Deleting a granted site removes the grant, never widens access."""
    engine = make_engine()
    with Session(engine) as session:
        enterprise, site, user = _member_ctx(session)
        membership = Membership(
            user_id=user.id,
            enterprise_id=enterprise.id,
            role="operator",
            has_all_sites=False,
        )
        membership.membership_sites.append(MembershipSite(site_id=site.id))
        session.add(membership)
        session.commit()

        session.delete(site)
        session.commit()
        session.refresh(membership)
        assert membership.membership_sites == []
        assert membership.has_all_sites is False


def test_user_and_enterprise_deletes_cascade_memberships():
    engine = make_engine()
    with Session(engine) as session:
        enterprise, site, user = _member_ctx(session)
        membership = Membership(
            user_id=user.id,
            enterprise_id=enterprise.id,
            role="admin",
            has_all_sites=False,
        )
        membership.membership_sites.append(MembershipSite(site_id=site.id))
        session.add(membership)
        session.commit()

        session.delete(user)
        session.commit()
        assert session.scalar(select(func.count(Membership.id))) == 0
        assert session.scalar(select(func.count(MembershipSite.id))) == 0

        other = User(email="other@example.com")
        session.add(other)
        session.flush()
        session.add(
            Membership(
                user_id=other.id, enterprise_id=enterprise.id, role="admin"
            )
        )
        session.commit()
        session.delete(enterprise)
        session.commit()
        assert session.scalar(select(func.count(Membership.id))) == 0
        assert session.get(User, other.id) is not None
