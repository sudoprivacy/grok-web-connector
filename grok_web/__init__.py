"""
Grok Web Connector - Python client for Grok Imagine web API.

Usage:
    from grok_web import GrokClient

    client = GrokClient()
    post = client.get_post("0c5c5864-fadb-440b-a52b-e441dab973d3")
    print(post.url)
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
from .models import GrokCookies, GrokPost, GrokVideo

__version__ = "0.1.0"

__all__ = [
    # Main client
    "GrokClient",
    # Models
    "GrokPost",
    "GrokVideo",
    "GrokCookies",
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
