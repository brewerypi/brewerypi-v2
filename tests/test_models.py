"""Example test — verifies the models and relationships work.

Test files use the `test_*.py` convention so pytest discovers them
automatically. This uses an in-memory SQLite database, so it needs no
setup or cleanup.
"""

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site


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
