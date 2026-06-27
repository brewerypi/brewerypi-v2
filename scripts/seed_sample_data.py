"""Seed the database with a little sample data.

Example of a developer script living in scripts/ with a snake_case name.
Run it with `python scripts/seed_sample_data.py` after the package is
installed (`pip install -e .`).
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.config import DATABASE_URL
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site


def main() -> None:
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        enterprise = Enterprise(abbreviation="BRW", name="Brewery Co")
        site = Site(abbreviation="HQ", name="Headquarters")
        site.areas.append(Area(abbreviation="MASH", name="Mash House"))
        site.areas.append(Area(abbreviation="FERM", name="Fermentation"))
        enterprise.sites.append(site)
        session.add(enterprise)
        session.commit()

    print("Seeded sample data.")


if __name__ == "__main__":
    main()
