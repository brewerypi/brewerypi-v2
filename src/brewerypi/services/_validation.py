"""Small shared validators for the service layer."""

import re

from brewerypi.services.exceptions import ValidationError

#: Separator used to build element attribute tag names, e.g.
#: "Cellar.FV01.Temperature". Names that become path segments may not
#: contain it, or the path would be ambiguous to read back.
TAG_PATH_SEPARATOR = "."

_WHITESPACE_RUN = re.compile(r"\s+")


def clean_str(value: str, field: str, max_len: int) -> str:
    """Strip a required string field and enforce a maximum length."""
    value = (value or "").strip()
    if not value:
        raise ValidationError(f"{field} is required")
    if len(value) > max_len:
        raise ValidationError(f"{field} exceeds {max_len} characters")
    return value


def clean_name_segment(value: str, field: str, max_len: int) -> str:
    """Clean a name that becomes a segment of a generated tag path.

    Trims, collapses internal whitespace runs to single spaces (so spaces are
    kept as typed -- "Hot Liquor Tank" stays readable), and rejects the tag
    path separator, which would make the generated path ambiguous.
    """
    value = clean_str(value, field, max_len)
    value = _WHITESPACE_RUN.sub(" ", value)
    if TAG_PATH_SEPARATOR in value:
        raise ValidationError(
            f"{field} may not contain {TAG_PATH_SEPARATOR!r} "
            "(it separates segments of generated tag names)"
        )
    return value


def optional_str(value: str | None) -> str | None:
    """Normalize an optional text field: None or blank becomes None."""
    if value is None:
        return None
    value = value.strip()
    return value or None
