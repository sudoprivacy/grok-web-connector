"""
Grok Web Connector - Python client for Grok Imagine web API.

    from grok_web import get_client

    async with get_client() as client:
        posts = await client.list_posts()
        video = await client.create_video("zoom in", source_post_id=post_id)

All APIs are public methods on GrokClient (see grok_web/client.py).
For parallel processing, see BrowserWorkerPool (grok_web/pool/).
"""

from pathlib import Path

from .auth import load_cookies, save_cookies
from .client import GrokClient
from .exceptions import (
    GrokAPIError,
    GrokAuthError,
    GrokConfigError,
    GrokError,
    GrokNotFoundError,
    GrokRateLimitError,
)
from .models import (
    ChildPost,
    GenerationMode,
    GrokCookies,
    ImageEditResult,
    ImageGenerationResult,
    ImageVideoMapping,
    PostDetails,
    PostSummary,
    VideoExtendResult,
    VideoGenerationResult,
    VideoMatchResult,
    VideoPreset,
)
from .pool import BrowserWorkerPool
from .selectors import select_all, signal_file_selector, timeout_selector

__version__ = "0.6.0"


def get_client(
    cookies: GrokCookies | None = None,
    config_path: Path | str | None = None,
    browser_host: str | None = None,
    browser_port: int | None = None,
    headless: bool = False,
) -> GrokClient:
    """
    Get the Grok API client.

    This is the recommended entry point for all Grok operations.

    Args:
        cookies: Pre-loaded GrokCookies (optional, loads from config if None)
        config_path: Path to config file (default: ~/.grok-config.json)
        browser_host: Chrome debugging host (optional, defaults to 127.0.0.1)
        browser_port: Chrome debugging port (optional, defaults to 9350)
        headless: Run browser in headless mode (default: False)

    Returns:
        GrokClient instance with all API methods.

    Example:
        async with get_client() as client:
            posts = await client.list_posts()
            await client.favorite_post(posts[0].id)
            video = await client.create_video("zoom in", source_post_id=posts[0].id, preset="fun")
    """
    return GrokClient(
        cookies=cookies,
        config_path=config_path,
        host=browser_host,
        port=browser_port,
        headless=headless,
    )


__all__ = [
    # Factory function (main entry point)
    "get_client",
    # Client class
    "GrokClient",
    # Worker Pool (for parallel processing)
    "BrowserWorkerPool",
    # Models
    "PostSummary",
    "PostDetails",
    "ChildPost",
    "GenerationMode",
    "GrokCookies",
    "ImageEditResult",
    "ImageGenerationResult",
    "ImageVideoMapping",
    "VideoMatchResult",
    "VideoExtendResult",
    "VideoGenerationResult",
    "VideoPreset",
    # Exceptions
    "GrokError",
    "GrokAuthError",
    "GrokAPIError",
    "GrokNotFoundError",
    "GrokConfigError",
    "GrokRateLimitError",
    # Auth utilities
    "load_cookies",
    "save_cookies",
    # Thumbnail selectors (for create_image)
    "select_all",
    "timeout_selector",
    "signal_file_selector",
]
