"""CDP network monitor for capturing Grok API responses.

Extracts the duplicated CDP monitoring pattern from GrokClient into
a reusable async context manager.
"""

import asyncio
import logging

from ..exceptions import GrokAPIError

logger = logging.getLogger(__name__)


class CDPMonitor:
    """Monitor CDP network events for requests matching a URL pattern.

    Captures request_id, response body, and x-statsig-id header from
    matching requests. Used to intercept Grok's /app-chat/conversations/new
    endpoint during video generation.

    Usage:
        async with CDPMonitor(tab, "/app-chat/conversations/new") as monitor:
            # ... trigger UI action that sends the request ...
            await monitor.wait_for_request()
            await monitor.wait_for_body(timeout=300)
            response_text = monitor.body
            statsig_id = monitor.statsig_id
    """

    def __init__(self, tab, url_pattern: str):
        self.tab = tab
        self.url_pattern = url_pattern
        self.request_id: str | None = None
        self.body: str | None = None
        self.statsig_id: str | None = None
        # Transport-level failure (TCP drop, abort, etc.) fires LoadingFailed,
        # NOT LoadingFinished — body stays None forever unless we also watch
        # that event. This field is set when CDP reports the matching
        # request's transport aborted; callers can tell "body lost to abort"
        # apart from "body genuinely not yet arrived".
        self.failed_reason: str | None = None
        self._active = False

    async def __aenter__(self):
        from ai_dev_browser import cdp

        await self.tab.send(cdp.network.enable())

        monitor = self  # closure reference

        async def _on_request(event: cdp.network.RequestWillBeSent):
            if not monitor._active:
                return
            url = event.request.url
            if monitor.url_pattern in url:
                monitor.request_id = event.request_id
                headers = event.request.headers
                if headers and (hasattr(headers, "get") or isinstance(headers, dict)):
                    monitor.statsig_id = headers.get("x-statsig-id")

        async def _on_loading_finished(event: cdp.network.LoadingFinished):
            if not monitor._active:
                return
            if monitor.request_id and monitor.request_id == event.request_id:
                try:
                    body_result = await monitor.tab.send(
                        cdp.network.get_response_body(request_id=event.request_id)
                    )
                    # CDP returns (body, base64_encoded) tuple
                    if isinstance(body_result, tuple):
                        body = body_result[0]
                    else:
                        body = getattr(body_result, "body", str(body_result))
                    monitor.body = body
                except Exception:
                    logger.debug("Failed to get response body for %s", event.request_id)

        async def _on_loading_failed(event: cdp.network.LoadingFailed):
            if not monitor._active:
                return
            if monitor.request_id and monitor.request_id == event.request_id:
                reason = getattr(event, "error_text", None) or getattr(
                    event, "blocked_reason", None
                )
                monitor.failed_reason = (
                    str(reason) if reason is not None else "LoadingFailed (no reason given)"
                )
                logger.info(
                    "CDPMonitor: request %s failed at transport — %s",
                    event.request_id,
                    monitor.failed_reason,
                )

        self.tab.add_handler(cdp.network.RequestWillBeSent, _on_request)
        self.tab.add_handler(cdp.network.LoadingFinished, _on_loading_finished)
        self.tab.add_handler(cdp.network.LoadingFailed, _on_loading_failed)
        self._active = True

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._active = False

    def reset(self):
        """Reset captured state for reuse (e.g., multi-step generation)."""
        self.request_id = None
        self.body = None
        # statsig_id is intentionally preserved across resets

    async def wait_for_request(self, timeout: float = 8) -> bool:
        """Wait for a matching request to be captured.

        Returns:
            True if request was captured, False if timed out
        """
        start = asyncio.get_event_loop().time()
        while self.request_id is None:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > timeout:
                return False
            await asyncio.sleep(0.5)
        return True

    async def wait_for_body(self, timeout: float = 300) -> str:
        """Wait for the response body to be captured.

        Args:
            timeout: Max seconds to wait

        Returns:
            Response body text

        Raises:
            GrokAPIError: If timed out
        """
        start = asyncio.get_event_loop().time()
        while self.body is None:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > timeout:
                raise GrokAPIError(f"Timeout ({timeout}s) waiting for response body")
            await asyncio.sleep(0.5)
        return self.body
