"""Seed the database with a little sample data.

Example of a developer script living in scripts/ with a snake_case name.
Run it with `python scripts/seed_sample_data.py` after the package is
installed (`pip install -e .`).
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.config import DATABASE_URL
from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Lookup, LookupValue, Site

SEED_DATA = [
    {
        "abbreviation": "NR",
        "name": "New Realm",
        "sites": [
            {
                "abbreviation": "ATL",
                "name": "Atlanta",
                "areas": [
                    ("BH", "Brewhouse"),
                    ("CL", "Cellar"),
                    ("PKG", "Packaging"),
                ],
            },
            {
                "abbreviation": "VB",
                "name": "Virginia Beach",
                "areas": [
                    ("BH", "Brewhouse"),
                    ("CL", "Cellar"),
                    ("PKG", "Packaging"),
                ],
            },
        ],
        "lookups": [
            {
                "name": "Yes / No",
                "values": [
                    ("Yes", True),
                    ("No", True),
                ],
            },
            {
                "name": "Brands",
                "values": [
                    ("Hazy Like a Fox", True),
                    ("Psychedelic Rabbit", True),
                    ("El Guapo", True),
                ],
            },
        ],
    },
    {
        "abbreviation": "DB",
        "name": "Deschutes Brewery",
        "sites": [
            {
                "abbreviation": "B1",
                "name": "Brew1",
                "areas": [
                    ("BH", "Brewhouse"),
                    ("CL", "Cellar"),
                    ("PKG", "Packaging"),
                ],
            },
            {
                "abbreviation": "B2",
                "name": "Brew2",
                "areas": [
                    ("BH", "Brewhouse"),
                    ("CL", "Cellar"),
                    ("PKG", "Packaging"),
                ],
            },
            {
                "abbreviation": "B3",
                "name": "Brew3",
                "areas": [
                    ("BH", "Brewhouse"),
                    ("CL", "Cellar"),
                    ("PKG", "Packaging"),
                ],
            },
        ],
        "lookups": [
            {
                "name": "Yes / No",
                "values": [
                    ("Yes", True),
                    ("No", True),
                ],
            },
            {
                "name": "Brands",
                "values": [
                    ("Fresh Squeezed", True),
                    ("Black Butte", True),
                    ("Mirror Pond", True),
                ],
            },
        ],
    },
]


def main() -> None:
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        for ent_data in SEED_DATA:
            enterprise = Enterprise(
                abbreviation=ent_data["abbreviation"],
                name=ent_data["name"],
            )
            for site_data in ent_data["sites"]:
                site = Site(
                    abbreviation=site_data["abbreviation"],
                    name=site_data["name"],
                )
                for abbr, name in site_data["areas"]:
                    site.areas.append(
                        Area(abbreviation=abbr, name=name)
                    )
                enterprise.sites.append(site)
            for lookup_data in ent_data["lookups"]:
                lookup = Lookup(name=lookup_data["name"])
                for name, is_selectable in lookup_data["values"]:
                    lookup.lookup_values.append(
                        LookupValue(
                            name=name,
                            is_selectable=is_selectable,
                        )
                    )
                enterprise.lookups.append(lookup)
            session.add(enterprise)
        session.commit()

    print("Seeded sample data.")


if __name__ == "__main__":
    main()
