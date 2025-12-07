"""Custom exceptions for Grok Web Connector."""


class GrokError(Exception):
    """Base exception for all Grok-related errors."""

    pass


class GrokAuthError(GrokError):
    """
    Raised when request is blocked (401/403).

    IMPORTANT: This usually means Cloudflare bot detection, NOT cookie expiration!

    Common causes (in order of likelihood):
    1. TLS fingerprint mismatch - curl_cffi impersonate version too old
    2. Headers mismatch - sec-ch-ua doesn't match current Chrome
    3. Cookie expiration - RARE, cookies typically last weeks/months

    Solution: Update impersonate version in client.py before asking user for new cookies.
    """

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
