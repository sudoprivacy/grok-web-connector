"""
Grok Web Connector - Python client for Grok Imagine web API.

Quick Start
===========
Use get_client() for all Grok API operations:

    from grok_web import get_client

    async with get_client() as client:
        # Read operations (HTTP - fast)
        posts = await client.list_posts()
        details = await client.get_post_details(post_id)

        # Favorite operations (HTTP + browser fallback)
        await client.favorite_post(post_id)
        await client.unfavorite_post(post_id)

        # Social operations (browser)
        await client.like_post(post_id)      # Thumbs up
        await client.dislike_post(post_id)   # Thumbs down

        # Image generation (browser) - txt2img with infinite scroll gallery
        images = await client.create_image("a sunset over mountains")

        # Video generation - unified API with automatic mode detection:
        #
        # 1. txt2vid - Generate video from text prompt only
        video = await client.create_video("a cat playing with yarn")

        # 2. img2vid - Generate video from existing Grok image
        video = await client.create_video(
            "zoom in slowly",
            source_post_id="abc-123-def",
            preset="fun"
        )

        # 3. img2vid with custom camera/motion instructions
        video = await client.create_video(
            "she turns her head, Pan Left, cinematic",
            source_post_id="abc-123-def"
        )

        # 4. upload2vid - Upload local image (NOT YET IMPLEMENTED)
        # video = await client.create_video(
        #     "make him smile",
        #     source_image_path="/path/to/photo.jpg"
        # )

        # Video management
        await client.upgrade_video(video_id)  # Adds hd_media_url to video
        await client.delete_video(video_id)

        # Image editing (browser)
        result = await client.edit_image(post_id, "add sunglasses")

Unified API
===========
All operations available through get_client():

Read Operations (HTTP - fast):
- list_posts()           - List saved posts or public feed
- get_post_details()     - Get full post details with children
- get_asset_file_size()  - Get file size from assets.grok.com
- validate_auth()        - Check if authentication is valid
- match_local_video()    - Match local file to web video

Favorite Operations (HTTP + browser fallback on 403):
- favorite_post()        - Add post to favorites (save)
- unfavorite_post()      - Remove from favorites

Social Operations (browser only):
- like_post()            - Give thumbs up
- dislike_post()         - Give thumbs down

Video Operations:
- create_video()           - Unified video generation API (auto-detects mode):
                             * No source → txt2vid (text prompt only)
                             * source_post_id → img2vid (from Grok image)
                             * source_image_path → upload2vid (NOT YET IMPLEMENTED)
                             Use preset='fun' for simple mode, or custom prompt
                             for camera movement, subject motion, and style
- upgrade_video()          - Upgrade to HD quality (adds hd_media_url)
- delete_video()           - Delete a child video (browser)

Image Operations (browser only):
- create_image()         - Generate images from text prompt (txt2img)
                           Returns gallery of images with infinite scroll
- edit_image()           - Edit existing image to generate variations

Parallel Processing
===================
BrowserWorkerPool for concurrent operations with multiple Chrome instances:

    from grok_web import BrowserWorkerPool

    async with BrowserWorkerPool(num_workers=3) as pool:
        # Test different camera movements on same image (img2vid)
        await pool.submit("create_video", prompt="Orbit", source_post_id="abc")
        await pool.submit("create_video", prompt="Pan Left", source_post_id="abc")
        await pool.submit("create_video", prompt="Static Shot", source_post_id="abc")
        results = await pool.wait()

Task types: create_video, create_image, favorite_post, unfavorite_post,
like_post, dislike_post, delete_video, upgrade_video, edit_image,
list_posts, get_post_details

Last updated: 2025-12-16
"""

from pathlib import Path

from .auth import load_cookies, save_cookies
from .browser import (
    find_nodriver_chromes,
    acquire_port_lock,
    release_port_lock,
    is_port_locked,
)
from .client import SmartGrokClient
from .selectors import select_all, timeout_selector, signal_file_selector
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
    ImageEditResult,
    ImageGenerationResult,
    PostDetails,
    PostSummary,
    VideoGenerationResult,
    VideoMatchResult,
    VideoPreset,
)
from .pool import BrowserWorkerPool

__version__ = "0.6.0"

# Track ports actively used by clients in this process
_active_ports: set[int] = set()


def _release_port(port: int) -> None:
    """Release a port back to the available pool and release the file lock."""
    _active_ports.discard(port)
    release_port_lock(port)


def get_client(
    cookies: GrokCookies | None = None,
    config_path: Path | str | None = None,
    browser_host: str | None = None,
    browser_port: int | None = None,
    headless: bool = False,
) -> SmartGrokClient:
    """
    Get the Grok API client.

    This is the recommended entry point for all Grok operations.
    Uses HTTP for fast read operations and automatically falls back
    to browser when needed (e.g., video creation blocked by 403).

    Port allocation strategy (when browser_port is not specified):
    1. First, try to reuse an existing Chrome with remote debugging enabled
    2. If none available, SmartGrokClient will auto-launch a new Chrome

    Args:
        cookies: Pre-loaded GrokCookies (optional, loads from config if None)
        config_path: Path to config file (default: ~/.grok-config.json)
        browser_host: Chrome debugging host (optional, auto-finds if not set)
        browser_port: Chrome debugging port (optional, auto-finds if not set)
        headless: Run browser in headless mode (default: False)

    Returns:
        Client instance with all API methods.

    Example:
        async with get_client() as client:
            posts = await client.list_posts()
            await client.favorite_post(posts[0].id)
            # img2vid with preset
            video = await client.create_video("zoom in", source_post_id=posts[0].id, preset="fun")
            # or txt2vid
            video = await client.create_video("a cat playing with yarn")
    """
    # Auto-find available Chrome if port not specified
    # Use exclude_locked=True to avoid stealing Chromes from other processes
    allocated_port = None
    if browser_port is None:
        available_ports = find_nodriver_chromes(exclude_locked=True)
        for port in available_ports:
            if port not in _active_ports:
                # Try to acquire file lock for cross-process coordination
                if acquire_port_lock(port):
                    browser_port = port
                    browser_host = browser_host or "127.0.0.1"
                    allocated_port = port
                    _active_ports.add(port)
                    break
                # Lock acquisition failed, try next port

    client = SmartGrokClient(
        cookies=cookies,
        config_path=config_path,
        browser_host=browser_host,
        browser_port=browser_port,
        browser_headless=headless,
    )

    # Store allocated port for release on exit
    if allocated_port is not None:
        client._allocated_port = allocated_port
        client._release_port_callback = _release_port

    return client


__all__ = [
    # Factory function (main entry point)
    "get_client",
    # Worker Pool (for parallel processing)
    "BrowserWorkerPool",
    # Models
    "PostSummary",
    "PostDetails",
    "ChildVideo",
    "GenerationMode",
    "GrokCookies",
    "ImageEditResult",
    "ImageGenerationResult",
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
    # Thumbnail selectors (for create_image)
    "select_all",
    "timeout_selector",
    "signal_file_selector",
    # Port lock utilities (for cross-process coordination)
    "acquire_port_lock",
    "release_port_lock",
    "is_port_locked",
]
