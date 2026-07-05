"""Service-layer CRUD for measurement units.

Each function takes an open Session and raises the service exceptions on
rule violations. Callers own the transaction (commit/rollback); these
functions ``flush`` so ids and integrity errors surface, but never commit.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from brewerypi.models import Enterprise, MeasurementUnit, Tag
from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


def list_measurement_units(
    session: Session, enterprise_id: int | None = None
) -> list[MeasurementUnit]:
    """Return measurement units, optionally filtered by enterprise."""
    stmt = select(MeasurementUnit).order_by(MeasurementUnit.name)
    if enterprise_id is not None:
        stmt = stmt.where(MeasurementUnit.enterprise_id == enterprise_id)
    return list(session.scalars(stmt).all())


def get_measurement_unit(session: Session, unit_id: int) -> MeasurementUnit:
    """Return one measurement unit, or raise NotFoundError."""
    unit = session.get(MeasurementUnit, unit_id)
    if unit is None:
        raise NotFoundError(f"no measurement unit with id {unit_id}")
    return unit


def create_measurement_unit(
    session: Session,
    enterprise_id: int,
    abbreviation: str,
    name: str,
    description: str | None = None,
) -> MeasurementUnit:
    """Create a measurement unit under an enterprise.

    Validates that the enterprise exists and that abbreviation and name are
    each unique within that enterprise.
    """
    abbreviation = _clean(abbreviation, "abbreviation", 10)
    name = _clean(name, "name", 45)
    if session.get(Enterprise, enterprise_id) is None:
        raise NotFoundError(f"no enterprise with id {enterprise_id}")
    _check_unique(session, enterprise_id, abbreviation, name)
    unit = MeasurementUnit(
        enterprise_id=enterprise_id,
        abbreviation=abbreviation,
        name=name,
        description=description,
    )
    session.add(unit)
    session.flush()
    return unit


def update_measurement_unit(
    session: Session,
    unit_id: int,
    abbreviation: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> MeasurementUnit:
    """Update a measurement unit; only provided fields change."""
    unit = get_measurement_unit(session, unit_id)
    new_abbr = unit.abbreviation
    new_name = unit.name
    if abbreviation is not None:
        new_abbr = _clean(abbreviation, "abbreviation", 10)
    if name is not None:
        new_name = _clean(name, "name", 45)
    _check_unique(
        session,
        unit.enterprise_id,
        new_abbr,
        new_name,
        exclude_id=unit_id,
    )
    unit.abbreviation = new_abbr
    unit.name = new_name
    if description is not None:
        unit.description = description
    session.flush()
    return unit


def delete_measurement_unit(session: Session, unit_id: int) -> None:
    """Delete a measurement unit, refusing if any tag references it."""
    unit = get_measurement_unit(session, unit_id)
    referencing = session.scalar(
        select(func.count())
        .select_from(Tag)
        .where(Tag.measurement_unit_id == unit_id)
    )
    if referencing:
        raise ValidationError(
            f"cannot delete measurement unit {unit_id}: "
            f"{referencing} tag(s) reference it"
        )
    session.delete(unit)
    session.flush()


def _clean(value: str, field: str, max_len: int) -> str:
    """Strip a string field and enforce required + max length."""
    value = (value or "").strip()
    if not value:
        raise ValidationError(f"{field} is required")
    if len(value) > max_len:
        raise ValidationError(f"{field} exceeds {max_len} characters")
    return value


def _check_unique(
    session: Session,
    enterprise_id: int,
    abbreviation: str,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Raise ConflictError if abbreviation or name is taken in the scope."""
    stmt = select(MeasurementUnit).where(
        MeasurementUnit.enterprise_id == enterprise_id,
        or_(
            MeasurementUnit.abbreviation == abbreviation,
            MeasurementUnit.name == name,
        ),
    )
    if exclude_id is not None:
        stmt = stmt.where(MeasurementUnit.id != exclude_id)
    existing = session.scalars(stmt).first()
    if existing is not None:
        field = (
            "abbreviation"
            if existing.abbreviation == abbreviation
            else "name"
        )
        raise ConflictError(
            f"a measurement unit with that {field} already exists "
            f"in enterprise {enterprise_id}"
        )
