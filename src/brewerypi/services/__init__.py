"""Service layer: reusable business logic for BreweryPi.

Functions here own validation and integrity rules so every consumer (the
MCP tools, a future web UI) shares one implementation. Callers open a
Session and own the transaction; service functions raise the exceptions
below. Each table gets its own module (e.g. ``measurement_units``).
"""

from brewerypi.services.areas import (
    create_area,
    delete_area,
    get_area,
    list_areas,
    update_area,
)
from brewerypi.services.element_attribute_templates import (
    create_element_attribute_template,
    delete_element_attribute_template,
    get_element_attribute_template,
    list_element_attribute_templates,
    update_element_attribute_template,
)
from brewerypi.services.element_templates import (
    create_element_template,
    delete_element_template,
    get_element_template,
    list_element_templates,
    update_element_template,
)
from brewerypi.services.elements import (
    create_element,
    delete_element,
    get_element,
    list_elements,
    update_element,
)
from brewerypi.services.enterprises import (
    create_enterprise,
    delete_enterprise,
    get_enterprise,
    list_enterprises,
    update_enterprise,
)
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
from brewerypi.services.sites import (
    create_site,
    delete_site,
    get_site,
    list_sites,
    update_site,
)
from brewerypi.services.tag_values import (
    delete_tag_value,
    get_tag_value,
    update_tag_value,
)
from brewerypi.services.tags import (
    create_tag,
    delete_tag,
    get_tag,
    list_tags,
    update_tag,
)

__all__ = [
    "ConflictError",
    "NotFoundError",
    "ServiceError",
    "ValidationError",
    "create_area",
    "create_element",
    "create_element_attribute_template",
    "create_element_template",
    "create_enterprise",
    "create_lookup",
    "create_lookup_value",
    "create_measurement_unit",
    "create_site",
    "create_tag",
    "delete_area",
    "delete_element",
    "delete_element_attribute_template",
    "delete_element_template",
    "delete_enterprise",
    "delete_lookup",
    "delete_lookup_value",
    "delete_measurement_unit",
    "delete_site",
    "delete_tag",
    "delete_tag_value",
    "get_area",
    "get_element",
    "get_element_attribute_template",
    "get_element_template",
    "get_enterprise",
    "get_lookup",
    "get_lookup_value",
    "get_measurement_unit",
    "get_site",
    "get_tag",
    "get_tag_value",
    "list_areas",
    "list_element_attribute_templates",
    "list_element_templates",
    "list_elements",
    "list_enterprises",
    "list_lookup_values",
    "list_lookups",
    "list_measurement_units",
    "list_sites",
    "list_tags",
    "update_area",
    "update_element",
    "update_element_attribute_template",
    "update_element_template",
    "update_enterprise",
    "update_lookup",
    "update_lookup_value",
    "update_measurement_unit",
    "update_site",
    "update_tag",
    "update_tag_value",
]
