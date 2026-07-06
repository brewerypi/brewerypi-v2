"""Small shared validators for the service layer."""

from brewerypi.services.exceptions import ValidationError


def clean_str(value: str, field: str, max_len: int) -> str:
    """Strip a required string field and enforce a maximum length."""
    value = (value or "").strip()
    if not value:
        raise ValidationError(f"{field} is required")
    if len(value) > max_len:
        raise ValidationError(f"{field} exceeds {max_len} characters")
    return value
