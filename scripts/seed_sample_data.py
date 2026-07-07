"""Seed the database with a little sample data.

Example of a developer script living in scripts/ with a snake_case name.
Run it with `python scripts/seed_sample_data.py` after the package is
installed (`pip install -e .`).
"""

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.config import DATABASE_URL
from brewerypi.database import Base
from brewerypi.models import (
    Area,
    Enterprise,
    Lookup,
    LookupValue,
    MeasurementUnit,
    Site,
    Tag,
    TagValue,
)

# Typical ambient °F ranges by area type.
_AMBIENT_TEMPS: dict[str, tuple[float, float]] = {
    "BH": (70.0, 85.0),   # Brewhouse: warm from boiling
    "CL": (34.0, 42.0),   # Cellar: cold fermentation
    "PKG": (55.0, 68.0),  # Packaging: moderately cool
}


def _ambient_temp(area_abbr: str) -> float:
    lo, hi = _AMBIENT_TEMPS.get(area_abbr, (60.0, 80.0))
    return round(random.uniform(lo, hi), 1)

SEED_DATA = [
    {
        "abbreviation": "NR",
        "name": "New Realm",
        "measurement_units": [
            ("°C", "Celsius"),
            ("°F", "Fahrenheit"),
            ("°P", "Plato"),
            ("bar", "Bar"),
            ("pH", "pH Units"),
        ],
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
        "measurement_units": [
            ("°C", "Celsius"),
            ("°F", "Fahrenheit"),
            ("°P", "Plato"),
            ("bar", "Bar"),
            ("pH", "pH Units"),
        ],
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

    # Noon UTC one week ago, stepping forward one day at a time.
    now = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    week_of_timestamps = [now - timedelta(days=i) for i in range(6, -1, -1)]

    with Session(engine) as session:
        for ent_data in SEED_DATA:
            enterprise = Enterprise(
                abbreviation=ent_data["abbreviation"],
                name=ent_data["name"],
            )
            fahrenheit_unit: MeasurementUnit | None = None
            for abbr, name in ent_data["measurement_units"]:
                mu = MeasurementUnit(abbreviation=abbr, name=name)
                enterprise.measurement_units.append(mu)
                if abbr == "°F":
                    fahrenheit_unit = mu
            for site_data in ent_data["sites"]:
                site = Site(
                    abbreviation=site_data["abbreviation"],
                    name=site_data["name"],
                )
                for abbr, name in site_data["areas"]:
                    area = Area(abbreviation=abbr, name=name)
                    tag = Tag(
                        name="Temperature",
                        measurement_unit=fahrenheit_unit,
                    )
                    for ts in week_of_timestamps:
                        tag.tag_values.append(
                            TagValue(
                                observed_at=ts,
                                value=_ambient_temp(abbr),
                            )
                        )
                    area.tags.append(tag)
                    site.areas.append(area)
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
