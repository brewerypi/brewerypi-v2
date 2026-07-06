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
from brewerypi.services.lookup_values import (
    create_lookup_value,
    delete_lookup_value,
    get_lookup_value,
    list_lookup_values,
    update_lookup_value,
)
from brewerypi.services.lookups import (
    create_lookup,
    delete_lookup,
    get_lookup,
    list_lookups,
    update_lookup,
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
    "create_lookup",
    "create_lookup_value",
    "create_measurement_unit",
    "delete_lookup",
    "delete_lookup_value",
    "delete_measurement_unit",
    "get_lookup",
    "get_lookup_value",
    "get_measurement_unit",
    "list_lookup_values",
    "list_lookups",
    "list_measurement_units",
    "update_lookup",
    "update_lookup_value",
    "update_measurement_unit",
]
