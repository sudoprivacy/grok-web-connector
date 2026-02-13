"""Custom exceptions for Grok Web Connector."""


class GrokError(Exception):
    """Base exception for all Grok-related errors."""

    pass


class GrokAuthError(GrokError):
    """Raised when request is blocked (401/403)."""

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


class GrokRateLimitError(GrokAPIError):
    """
    Raised when Grok API returns "Too many requests" (error code 8).

    IMPORTANT: Rate limits are GLOBAL, not per-request!

    When you encounter this error:
    1. DO NOT immediately retry - this wastes retry attempts
    2. STOP all workers/requests and wait globally
    3. As of December 2025, Grok rate limits reset every hour

    Recommended handling:
    - Catch this exception at the pool/orchestrator level
    - Pause all workers for 5-10 minutes minimum
    - Consider implementing exponential backoff
    - If persistent, wait until the next hour boundary

    Example:
        try:
            result = await client.create_video(...)
        except GrokRateLimitError:
            # Stop all workers, wait globally
            await asyncio.sleep(600)  # Wait 10 minutes
            # Or wait until next hour if near boundary
    """

    pass
