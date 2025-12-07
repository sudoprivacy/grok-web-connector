"""Custom exceptions for Grok Web Connector."""


class GrokError(Exception):
    """Base exception for all Grok-related errors."""

    pass


class GrokAuthError(GrokError):
    """Raised when authentication fails (invalid or expired cookies)."""

    pass


class GrokAPIError(GrokError):
    """Raised when API request fails."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GrokNotFoundError(GrokAPIError):
    """Raised when a resource is not found (404)."""

    pass


class GrokConfigError(GrokError):
    """Raised when configuration is invalid or missing."""

    pass
