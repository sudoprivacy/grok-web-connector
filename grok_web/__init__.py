"""
Grok Web Connector - Python client for Grok Imagine web API.

5 Core APIs:
    1. list_posts()           - Scan and get overview of all posts
    2. get_post_details()     - Get full details for a specific post
    3. get_asset_file_size()  - Get file size from assets.grok.com URL
    4. validate_auth()        - Check if authentication is valid
    5. match_local_video()    - Match local file to web video, generate new filename

Usage:
    from grok_web import GrokClient, GenerationMode

    client = GrokClient()

    # Scan all posts
    posts = client.list_posts(limit=10)
    for p in posts:
        print(f"{p.id}: {p.mode.value} ({p.video_count} videos)")

    # Get details for a specific post
    details = client.get_post_details("0c5c5864-fadb-440b-a52b-e441dab973d3")
    print(f"Mode: {details.mode}")
    for child in details.children:
        print(f"  Child: {child.id}")

    # Match local video to web and get new filename
    result = client.match_local_video("/path/to/grok-video-xxx.mp4")
    print(f"New filename: {result.new_filename}")
"""

from .auth import load_cookies, save_cookies
from .client import GrokClient
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
    # Main client
    "GrokClient",
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
