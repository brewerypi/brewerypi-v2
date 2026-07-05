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


class ConflictError(ServiceError):
    """The operation would break a uniqueness or integrity constraint."""
