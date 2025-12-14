"""
Grok Web Connector - Python client for Grok Imagine web API.

Quick Start (Recommended)
=========================
Use SmartGrokClient via get_client() for best performance:

    from grok_web import get_client

    # All operations work out of the box - Chrome auto-launches if needed!
    async with get_client() as client:
        posts = await client.list_posts()  # HTTP (fast)
        video = await client.create_video(post_id, preset="fun")  # Auto browser fallback

SmartGrokClient advantages:
- HTTP for read operations (fast, no browser overhead)
- Auto-launches isolated Chrome when needed (no manual setup!)
- Browser stays open between calls for maximum speed
- Lazy browser initialization (only when video creation is blocked)

Client Implementations
======================
1. SmartGrokClient (Recommended - use via get_client())
   - HTTP for reads, auto browser fallback when blocked
   - Best of both worlds: speed + reliability
   - Chrome auto-launches if needed (isolated profile)
   - Use: async with get_client() as client:

2. NodriverClient (Direct browser control)
   - Full browser automation via Chrome DevTools Protocol
   - Use when you need direct browser access
   - Use: async with NodriverClient(port=9222) as client:

3. BrowserWorkerPool (Parallel processing)
   - Multiple concurrent browser workers
   - Job queue with progress persistence
   - Use: async with BrowserWorkerPool(num_workers=3) as pool:

Factory Functions
=================
get_client(browser_host, browser_port, ...)  -> SmartGrokClient (recommended)
get_sync_client(...)                         -> PlaywrightClient or GrokClient

Core APIs
=========
Read APIs (via HTTP - fast):
1. list_posts()              - List liked posts (default) or all public posts
2. get_post_details()        - Get full details for a specific post
3. get_asset_file_size()     - Get file size from assets.grok.com URL
4. validate_auth()           - Check if authentication is valid
5. match_local_video()       - Match local file to web video

Write APIs:
6. like_post()               - Save post to favorites
7. unlike_post()             - Remove post from favorites
8. create_video()            - Generate video (HTTP first, browser fallback)
9. create_video_from_image() - Direct API call (may be blocked with 403)

UI Menu Operations (NodriverClient only):
10. delete_video_via_ui()    - Delete a video via UI click
11. save_post_via_ui()       - Save post to favorites (toggle)
12. unsave_post_via_ui()     - Remove from favorites
13. like_post_via_ui()       - Like a post
14. dislike_post_via_ui()    - Dislike a post
15. upgrade_video_via_ui()   - Convert non-HD video to HD
16. get_menu_items()         - List available menu options

Parallel Processing
===================
BrowserWorkerPool - for concurrent video generation with multiple Chrome instances:

    from grok_web.pool import BrowserWorkerPool

    async with BrowserWorkerPool(num_workers=3, state_file="progress.json") as pool:
        # Submit jobs
        for command in ["Orbit", "Pan Left", "Static Shot"]:
            await pool.submit("create_video", post_id="abc123", adjustment_prompt=command)

        # Wait for all
        results = await pool.wait_all()

Features:
- Multiple concurrent workers on separate Chrome ports
- Dynamic scaling: add/remove workers at runtime
- Progress persistence: resume after restart
- Graceful shutdown: complete current tasks before exit

Last updated: 2025-12-14
"""

from pathlib import Path

from .auth import load_cookies, save_cookies
from .client import (
    AsyncClient,
    GrokClient,
    NodriverClient,
    PlaywrightClient,
    SmartGrokClient,
)
from .exceptions import (
    GrokAPIError,
    GrokAuthError,
    GrokConfigError,
    GrokError,
    GrokNotFoundError,
)
from .models import (
    ChildVideo,
    GenerationMode,
    GrokCookies,
    PostDetails,
    PostSummary,
    VideoGenerationResult,
    VideoMatchResult,
    VideoPreset,
)
from .pool import BrowserWorkerPool

__version__ = "0.5.0"


def get_client(
    cookies: GrokCookies | None = None,
    config_path: Path | str | None = None,
    browser_host: str | None = None,
    browser_port: int | None = None,
    headless: bool = False,
) -> SmartGrokClient:
    """
    Get the recommended async client for Grok API.

    Returns SmartGrokClient which uses HTTP for read operations and
    lazy browser initialization for video creation (when blocked).

    Args:
        cookies: Pre-loaded GrokCookies (optional, loads from config if None)
        config_path: Path to config file (default: ~/.grok-config.json)
        browser_host: Chrome remote debugging host (e.g., "127.0.0.1")
                      Required for video creation fallback.
        browser_port: Chrome remote debugging port (e.g., 9222)
                      Required for video creation fallback.
        headless: Run browser in headless mode (default: False)

    Returns:
        SmartGrokClient instance.

    Example (Read-only operations - no browser needed):
        async with get_client() as client:
            posts = await client.list_posts()  # HTTP (fast)
            result = await client.match_local_video(path)  # HTTP (fast)

    Example (Video creation with browser fallback):
        # Terminal 1: Start Chrome once
        # /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222

        # Python: HTTP for reads, browser fallback for video
        async with get_client(browser_host="127.0.0.1", browser_port=9222) as client:
            posts = await client.list_posts()  # HTTP (fast)
            video = await client.create_video(post_id, preset="fun")  # Browser fallback
    """
    return SmartGrokClient(
        cookies=cookies,
        config_path=config_path,
        browser_host=browser_host,
        browser_port=browser_port,
        browser_headless=headless,
    )


def get_sync_client(
    cookies: GrokCookies | None = None,
    config_path: Path | str | None = None,
) -> PlaywrightClient | GrokClient:
    """
    Get the best available sync client for Grok API.

    Args:
        cookies: Pre-loaded GrokCookies (optional, loads from config if None)
        config_path: Path to config file (default: ~/.grok-config.json)

    Returns:
        PlaywrightClient if playwright is available, otherwise GrokClient.

    Example:
        with get_sync_client() as client:
            posts = client.list_posts()

    Note:
        For video generation, prefer get_client() (async) with persistent Chrome
        for better Cloudflare handling and faster performance.
    """
    # Try PlaywrightClient first (more reliable)
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        return PlaywrightClient(cookies=cookies, config_path=config_path)
    except ImportError:
        pass

    # Fall back to GrokClient (curl_cffi)
    return GrokClient(cookies=cookies, config_path=config_path)


__all__ = [
    # Factory functions (recommended)
    "get_client",
    "get_sync_client",
    # Clients
    "SmartGrokClient",  # Recommended: HTTP + browser fallback
    "NodriverClient",  # Direct browser automation
    # Worker Pool
    "BrowserWorkerPool",
    # Models
    "PostSummary",
    "PostDetails",
    "ChildVideo",
    "GenerationMode",
    "GrokCookies",
    "VideoMatchResult",
    "VideoGenerationResult",
    "VideoPreset",
    # Exceptions
    "GrokError",
    "GrokAuthError",
    "GrokAPIError",
    "GrokNotFoundError",
    "GrokConfigError",
    # Auth utilities
    "load_cookies",
    "save_cookies",
]

# Internal clients (not recommended for direct use):
# - AsyncClient: HTTP-only, no browser fallback. Used internally by SmartGrokClient.
# - PlaywrightClient: Sync HTTP, no fallback. Used by get_sync_client().
# - GrokClient: Sync curl_cffi, no fallback. Used by get_sync_client().
