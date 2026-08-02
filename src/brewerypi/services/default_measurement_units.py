"""The default measurement units a new enterprise starts with.

Ported from upstream BreweryPi's ``addDefaultUnitsOfMeasurements`` admin
route, with the names and symbols normalized:

* Names are singular unless the term is idiomatically plural ("parts per
  million", "cells per milliliter", "volumes of CO2"), sentence case, with
  proper nouns capitalized ("degree Celsius", not "degree celsius").
* Symbols follow SI style: liter is a capital ``L`` everywhere (upstream
  mixed ``mL``/``g/L`` with ``cells/ml``) and every derived unit uses the
  solidus (``gal/min``, not ``gpm``).
* Names spell out what upstream left ambiguous: which barrel, which gallon,
  which ton, gauge vs absolute pressure, and that EBC/SRM are color scales.
  Upstream's ``ASBC`` is dropped -- it names an organization, and its color
  method IS the Standard Reference Method already in the list.
* The metric counterparts upstream omitted are included (hL, degree Brix,
  bar, mg/L, degree Celsius per minute, L/min, g/hL, kg/hL): the upstream
  list has ``bbl`` but no ``hL``, ``psi`` but no ``bar``, ``degree
  Fahrenheit per minute`` but no Celsius equivalent.

Note that several entries (ADF, RDF, RE, TA, SG, and the color scales) are
analyses or dimensionless ratios rather than true units. They are kept
because the unit label is what tells an operator which scale a number is on.

Symbols fit ``MeasurementUnit.abbreviation`` (10 chars) and names fit
``name`` (45); ``test_services_default_measurement_units.py`` guards that.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from brewerypi.models import Enterprise, MeasurementUnit
from brewerypi.services.exceptions import NotFoundError

#: ``(abbreviation, name)`` pairs, ordered by name.
DEFAULT_MEASUREMENT_UNITS: tuple[tuple[str, str], ...] = (
    ("ADF", "Apparent degree of fermentation"),
    ("bar", "Bar"),
    ("cells/mL", "Cells per milliliter"),
    ("cells/mL°P", "Cells per milliliter per degree Plato"),
    ("°Bx", "Degree Brix"),
    ("°C", "Degree Celsius"),
    ("°C/min", "Degree Celsius per minute"),
    ("°F", "Degree Fahrenheit"),
    ("°F/min", "Degree Fahrenheit per minute"),
    ("°P", "Degree Plato"),
    ("EBC", "European Brewery Convention color"),
    ("g", "Gram"),
    ("g/bbl", "Gram per barrel"),
    ("g/hL", "Gram per hectoliter"),
    ("g/L", "Gram per liter"),
    ("hL", "Hectoliter"),
    ("h", "Hour"),
    ("in", "Inch"),
    ("IBU", "International bitterness unit"),
    ("kg", "Kilogram"),
    ("kg/hL", "Kilogram per hectoliter"),
    ("L", "Liter"),
    ("L/min", "Liter per minute"),
    ("t/h", "Metric ton per hour"),
    ("mg", "Milligram"),
    ("mg/L", "Milligram per liter"),
    ("mL", "Milliliter"),
    ("mm", "Millimeter"),
    ("10⁶ cells", "Million cells"),
    ("min", "Minute"),
    ("ppb", "Parts per billion"),
    ("ppm", "Parts per million"),
    ("%", "Percent"),
    ("pH", "pH"),
    ("lb", "Pound"),
    ("lb/bbl", "Pound per barrel"),
    ("psi", "Pound per square inch"),
    ("psig", "Pound per square inch gauge"),
    ("RDF", "Real degree of fermentation"),
    ("RE", "Real extract"),
    ("s", "Second"),
    ("SG", "Specific gravity"),
    ("SRM", "Standard Reference Method color"),
    ("TA", "Total acidity"),
    ("10¹² cells", "Trillion cells"),
    ("bbl", "US beer barrel (31 gal)"),
    ("gal", "US gallon"),
    ("gal/min", "US gallon per minute"),
    ("vol", "Volumes of CO2"),
)


def add_default_measurement_units(
    session: Session, enterprise_id: int
) -> list[MeasurementUnit]:
    """Add the default measurement units to an enterprise.

    Idempotent, like the upstream route: a default whose abbreviation OR
    name is already taken in the enterprise is skipped rather than raising,
    so this can safely backfill an enterprise that already has units.
    Returns the units it created (empty if all were already present).
    """
    if session.get(Enterprise, enterprise_id) is None:
        raise NotFoundError(f"no enterprise with id {enterprise_id}")
    existing = session.scalars(
        select(MeasurementUnit).where(
            MeasurementUnit.enterprise_id == enterprise_id
        )
    ).all()
    abbreviations = {u.abbreviation for u in existing}
    names = {u.name for u in existing}
    added: list[MeasurementUnit] = []
    for abbreviation, name in DEFAULT_MEASUREMENT_UNITS:
        if abbreviation in abbreviations or name in names:
            continue
        unit = MeasurementUnit(
            enterprise_id=enterprise_id,
            abbreviation=abbreviation,
            name=name,
        )
        session.add(unit)
        added.append(unit)
        abbreviations.add(abbreviation)
        names.add(name)
    session.flush()
    return added
