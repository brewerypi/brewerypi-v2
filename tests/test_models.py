"""Tests for all domain models — verifies relationships, navigation, and
cascade deletes. Uses an in-memory SQLite database, so no setup or cleanup
is required.
"""

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Lookup, LookupValue, Site


def make_engine():
    engine = create_engine("sqlite:///:memory:")
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
