"""
Grok Web Connector - Python client for Grok Imagine web API.

Two client implementations:
    - GrokClient: Uses curl_cffi (works on macOS, may fail on Windows)
    - GrokPlaywrightClient: Uses Playwright (recommended for Windows)

5 Core APIs (same for both clients):
    1. list_posts()           - Scan and get overview of all posts
    2. get_post_details()     - Get full details for a specific post
    3. get_asset_file_size()  - Get file size from assets.grok.com URL
    4. validate_auth()        - Check if authentication is valid
    5. match_local_video()    - Match local file to web video, generate new filename

Usage:
    # On macOS (curl_cffi usually works):
    from grok_web import GrokClient
    client = GrokClient()

    # On Windows (use Playwright for reliable Cloudflare bypass):
    from grok_web import GrokPlaywrightClient
    with GrokPlaywrightClient() as client:
        posts = client.list_posts(limit=10)
        for p in posts:
            print(f"{p.id}: {p.mode.value} ({p.video_count} videos)")

    # Match local video to web and get new filename
    result = client.match_local_video("/path/to/grok-video-xxx.mp4")
    print(f"New filename: {result.new_filename}")
"""

from .auth import load_cookies, save_cookies
from .client import GrokClient
from .playwright_client import GrokPlaywrightClient, GrokAsyncPlaywrightClient
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
    VideoMatchResult,
)

__version__ = "0.2.0"

__all__ = [
    # Clients
    "GrokClient",
    "GrokPlaywrightClient",
    "GrokAsyncPlaywrightClient",
    # Models
    "PostSummary",
    "PostDetails",
    "ChildVideo",
    "GenerationMode",
    "GrokCookies",
    "VideoMatchResult",
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
