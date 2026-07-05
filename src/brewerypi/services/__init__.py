"""Service layer: reusable business logic for BreweryPi.

Functions here own validation and integrity rules so every consumer (the
MCP tools, a future web UI) shares one implementation. Callers open a
Session and own the transaction; service functions raise the exceptions
below. Each table gets its own module (e.g. ``measurement_units``).
"""

from brewerypi.services.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationError,
)
from brewerypi.services.measurement_units import (
    create_measurement_unit,
    delete_measurement_unit,
    get_measurement_unit,
    list_measurement_units,
    update_measurement_unit,
)

__all__ = [
    "ConflictError",
    "NotFoundError",
    "ServiceError",
    "ValidationError",
    "create_measurement_unit",
    "delete_measurement_unit",
    "get_measurement_unit",
    "list_measurement_units",
    "update_measurement_unit",
]
