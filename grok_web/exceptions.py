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


class GrokGenerationFailedError(GrokAPIError):
    """Raised when Grok's response looks structurally valid but the
    generation did not actually produce a real post.

    Examples:
    - Response contains a ``streamingVideoGenerationResponse`` block,
      including a valid-format ``videoId``, but no actual extension
      happened (``cumulative_duration`` didn't grow past the source's
      chain tail). Grok's server probably pre-filtered the prompt /
      reference frame before the generation pipeline produced output.
    - Returned ``video_id`` is not fetchable via
      ``/rest/media/post/get`` — Grok never persisted it.

    Callers should treat this as retryable, but with the understanding
    that the same prompt may reproduce the failure. Varying the prompt
    phrasing or seed usually clears it.
    """

    pass


class GrokModerationError(GrokAPIError):
    """Raised when Grok declines to generate and answers in CHAT mode instead.

    Video generation (and other media gen) normally streams back a
    ``streamingVideoGenerationResponse``. When Grok instead returns a plain
    chat reply (``result.userResponse`` with a ``message``, no media block),
    the generation pipeline never engaged. This describes the BEHAVIOR; the
    cause is for the caller to diagnose in the UI. Observed causes include:

    - **Content moderation** — the prompt / source frame blocked pre-flight.
    - **Profile/account-scoped restriction** — the same source that generates
      fine in a user's normal browser can have its Make-Video ('制作视频')
      button ABSENT in an automation browser profile (anti-bot / trust /
      feature-flag state). Open the post URL in the UI to check.

    (An earlier version asserted "unavailable/removed source" as the cause; a
    consumer disproved that e2e — the source image loaded fine at full
    resolution, only the button was missing in the connector's profile.)

    This is NOT a parser bug (the connector used to raise a confusing
    "Failed to parse video generation response" here). Retrying the SAME
    source in the SAME profile is unlikely to help. The offending chat
    ``message`` is on ``.chat_message`` for inspection.
    """

    def __init__(self, message: str, chat_message: str | None = None):
        super().__init__(message)
        self.chat_message = chat_message


class GrokRateLimitError(GrokAPIError):
    """
    Raised when Grok rate-limits or anti-abuse-throttles the session.

    Detection sources (any one):
    - NDJSON error with code 8 / "too many requests" text
    - Visible UI banner matching rate-limit patterns
      ("请稍后" / "稍候" / "频繁" / "rate limit" / "too many" / "try again later")

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


class GrokQuotaExceededError(GrokRateLimitError):
    """Raised when the session hits a daily / billing-period quota limit.

    Distinct from :class:`GrokRateLimitError` (transient throttle) —
    quota errors mean this account is done generating until the next
    reset window (typically 24 hours). Do NOT retry on a backoff loop;
    stop the run and wait for the quota cycle.

    Detection: UI banners matching patterns like "今日生成已达上限",
    "daily limit", "quota exceeded".

    Inherits from :class:`GrokRateLimitError` so code that catches the
    parent still handles this; catch :class:`GrokQuotaExceededError`
    specifically when you want "stop, don't retry" semantics.

    Example:
        try:
            result = await client.extend_video(...)
        except GrokQuotaExceededError:
            # Hard stop — retrying won't help for ~24h
            logger.error("Quota exceeded; aborting batch")
            return
        except GrokRateLimitError:
            # Soft throttle — back off and retry later
            await asyncio.sleep(600)
    """

    pass
