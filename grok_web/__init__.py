"""
Grok Web Connector - Python client for Grok Imagine web API.

Client Implementations
======================
Three client classes are available:

GrokClient (curl_cffi)
    - Uses curl_cffi library with Chrome TLS fingerprint impersonation
    - Lightweight, fast startup
    - Works well on macOS/Linux
    - May fail on Windows due to TLS fingerprint mismatch with Cloudflare

GrokPlaywrightClient (Playwright sync)
    - Uses Playwright's APIRequestContext with native Chromium TLS
    - Reliable Cloudflare bypass on all platforms
    - Slightly slower startup (Playwright initialization)
    - Use in synchronous code

GrokAsyncPlaywrightClient (Playwright async)
    - Async version of GrokPlaywrightClient
    - Use in async contexts (MCP servers, asyncio applications)

Which Client to Use?
====================
- macOS/Linux: Start with GrokClient. If you get 403 errors, switch to Playwright.
- Windows: Use GrokPlaywrightClient or GrokAsyncPlaywrightClient directly.
- MCP servers: Use GrokAsyncPlaywrightClient (required for async context).

Cookie Refresh
==============
All clients require a cf_clearance cookie from Cloudflare. This cookie:
- Is obtained when you visit grok.com in a real browser
- Binds to your browser's TLS fingerprint
- Expires periodically and needs manual refresh

To refresh cookies, run: python refresh_cf_clearance.py

5 Core APIs (same for all clients)
==================================
1. list_posts()           - Scan and get overview of all posts
2. get_post_details()     - Get full details for a specific post
3. get_asset_file_size()  - Get file size from assets.grok.com URL
4. validate_auth()        - Check if authentication is valid
5. match_local_video()    - Match local file to web video, generate new filename

Usage Examples
==============
# Sync client (macOS/Linux)
from grok_web import GrokClient
client = GrokClient()
posts = client.list_posts(limit=10)

# Sync Playwright client (Windows or when curl_cffi fails)
from grok_web import GrokPlaywrightClient
with GrokPlaywrightClient() as client:
    posts = client.list_posts(limit=10)

# Async Playwright client (MCP servers, async code)
from grok_web import GrokAsyncPlaywrightClient
async with GrokAsyncPlaywrightClient() as client:
    posts = await client.list_posts(limit=10)
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

__version__ = "0.3.0"

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
