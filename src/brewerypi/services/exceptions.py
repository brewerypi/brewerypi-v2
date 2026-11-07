"""Exceptions raised by the service layer.

Service functions raise these on business-rule violations so that each
caller (MCP tools, a future web UI) can translate them into its own error
shape without duplicating the rules themselves.
"""


class ServiceError(Exception):
    """Base class for all service-layer errors."""


class NotFoundError(ServiceError):
    """A referenced record does not exist."""


class ValidationError(ServiceError):
    """Input violated a business rule."""


class NotSelectableError(ValidationError):
    """A text value that isn't a selectable option on the tag's list.

    ``allowed`` names the options that are, so a caller can offer them.
    """

    def __init__(self, message: str, allowed: list[str]):
        super().__init__(message)
        self.allowed = allowed


class ConflictError(ServiceError):
    """The operation would break a uniqueness or integrity constraint."""


class PermissionDeniedError(ServiceError):
    """The caller is not allowed to do this (or to sign in at all)."""
