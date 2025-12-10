"""
Grok Web Connector - Python client for Grok Imagine web API.

Client Implementations
======================
Three client classes are available:

GrokClient (recommended)
    - Uses curl_cffi library with Chrome TLS fingerprint impersonation
    - Lightweight, fast startup
    - Works well on macOS/Linux

PlaywrightClient
    - Uses Playwright's APIRequestContext with native Chromium TLS
    - Reliable Cloudflare bypass on all platforms
    - Use as context manager: with PlaywrightClient() as client:

AsyncClient
    - Async version using Playwright
    - For async contexts (MCP servers, asyncio applications)
    - Use as async context manager: async with AsyncClient() as client:

Which Client to Use?
====================
- macOS/Linux: Start with GrokClient. If you get 403 errors, switch to PlaywrightClient.
- Windows: Use PlaywrightClient or AsyncClient directly.
- MCP servers: Use AsyncClient (required for async context).

Cookie Refresh
==============
All clients require a cf_clearance cookie from Cloudflare. This cookie:
- Is obtained when you visit grok.com in a real browser
- Binds to your browser's TLS fingerprint
- Expires periodically and needs manual refresh

To refresh cookies, run: python refresh_cf_clearance.py

8 Core APIs (same for all clients)
==================================
Read APIs:
1. list_posts()              - List your liked posts (default) or all public posts
2. get_post_details()        - Get full details for a specific post
3. get_asset_file_size()     - Get file size from assets.grok.com URL
4. validate_auth()           - Check if authentication is valid
5. match_local_video()       - Match local file to web video, generate new filename

Write APIs:
6. like_post()               - Save post to favorites (enables long-term persistence)
7. unlike_post()             - Remove post from favorites (equivalent to delete)
8. create_video_from_image() - Generate video from image via Grok chat API

API Test Status
===============
| #  | API                      | Tested | Notes                              |
|----|--------------------------|--------|------------------------------------|
| 1  | list_posts()             | YES    | Production-proven                  |
| 2  | get_post_details()       | YES    | Production-proven                  |
| 3  | get_asset_file_size()    | YES    | Production-proven                  |
| 4  | validate_auth()          | YES    | Production-proven                  |
| 5  | match_local_video()      | YES    | Production-proven                  |
| 6  | like_post()              | NO     | Added 2025-12-10, needs testing    |
| 7  | unlike_post()            | NO     | Added 2025-12-10, needs testing    |
| 8  | create_video_from_image()| NO     | Added 2025-12-10, needs testing    |

Last updated: 2025-12-11

Usage Examples
==============
# Sync client (macOS/Linux)
from grok_web import GrokClient
client = GrokClient()
posts = client.list_posts(limit=10)  # Returns your liked posts by default
all_public = client.list_posts(limit=10, source=None)  # All public posts

# Sync Playwright client (Windows or when curl_cffi fails)
from grok_web import PlaywrightClient
with PlaywrightClient() as client:
    posts = client.list_posts(limit=10)  # Your liked posts

# Async Playwright client (MCP servers, async code)
from grok_web import AsyncClient
async with AsyncClient() as client:
    posts = await client.list_posts(limit=10)
"""

from .auth import load_cookies, save_cookies
from .client import AsyncClient, GrokClient, PlaywrightClient
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

__version__ = "0.4.0"

__all__ = [
    # Clients
    "GrokClient",
    "PlaywrightClient",
    "AsyncClient",
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
