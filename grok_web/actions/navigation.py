"""Navigation actions for Grok Imagine post pages."""

import asyncio
import logging

from ..exceptions import GrokAPIError

logger = logging.getLogger(__name__)

BASE_URL = "https://grok.com"


async def navigate_to_post(tab, post_id: str, *, delay: float = 1.0) -> None:
    """Navigate to a Grok Imagine post page and wait for load.

    Args:
        tab: browser Tab instance
        post_id: Post/video UUID
        delay: UI delay multiplier

    Raises:
        GrokAPIError: If post returns 404
    """
    url = f"{BASE_URL}/imagine/post/{post_id}"
    await tab.get(url)
    await asyncio.sleep(3 * delay)

    # Check for 404
    page_text = await tab.evaluate("document.body.innerText")
    if page_text and ("Page not found" in page_text or "404" in page_text):
        raise GrokAPIError(f"Post {post_id} not found (404)")


def is_on_post_page(current_url: str, post_id: str) -> bool:
    """Check if currently on the expected post page.

    Args:
        current_url: Current browser URL
        post_id: Expected post UUID
    """
    return f"/imagine/post/{post_id}" in current_url
