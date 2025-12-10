"""
Grok Web Connector - API Clients

Three client implementations for different use cases:

GrokClient (recommended)
    - Uses curl_cffi with Chrome TLS fingerprint impersonation
    - Best for macOS/Linux
    - Lightweight, fast startup

PlaywrightClient
    - Uses Playwright's native Chromium TLS
    - Reliable Cloudflare bypass on all platforms
    - Use when GrokClient fails with 403 errors
    - Must be used as context manager: with PlaywrightClient() as client:

AsyncClient
    - Async version using Playwright
    - For async contexts (MCP servers, asyncio applications)
    - Must be used as async context manager: async with AsyncClient() as client:
"""

import re
from pathlib import Path
from typing import Any

from curl_cffi import requests
from playwright.async_api import (
    APIRequestContext as AsyncAPIRequestContext,
)
from playwright.async_api import (
    Playwright as AsyncPlaywright,
)
from playwright.async_api import (
    async_playwright,
)
from playwright.sync_api import APIRequestContext, Playwright, sync_playwright

from ._internal import ResponseParser, SyncClientBase
from .auth import get_platform_headers, load_config
from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from .models import (
    GenerationMode,
    GrokCookies,
    PostDetails,
    PostSummary,
    VideoMatchResult,
)

# =============================================================================
# Helper functions
# =============================================================================


def _get_browser_headers() -> dict[str, str]:
    """Get headers that match Playwright's Chromium browser."""
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }


# =============================================================================
# GrokClient - Default client using curl_cffi
# =============================================================================


class GrokClient(SyncClientBase):
    """
    Default Grok API client using curl_cffi.

    Uses Chrome TLS fingerprint impersonation to bypass Cloudflare.
    Best for macOS/Linux. If you get 403 errors, try PlaywrightClient.

    Example:
        >>> client = GrokClient()
        >>> posts = client.list_posts(limit=10)
        >>> details = client.get_post_details(posts[0].id)
    """

    BASE_API_HEADERS = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "sec-ch-ua-mobile": "?0",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }

    BASE_ASSET_HEADERS = {
        "referer": "https://grok.com/",
        "origin": "https://grok.com",
    }

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
    ):
        """
        Initialize Grok client.

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Defaults to ~/.grok-config.json
        """
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]
            custom_headers = config["headers"]
        else:
            custom_headers = {}

        self.cookies = cookies

        platform_headers = get_platform_headers()

        self._api_headers = {
            **self.BASE_API_HEADERS,
            **platform_headers,
            **custom_headers,
        }

        self._asset_headers = {
            **self.BASE_ASSET_HEADERS,
            "user-agent": platform_headers["user-agent"],
            **{k: v for k, v in custom_headers.items() if k == "user-agent"},
        }

        self._session = requests.Session(impersonate="chrome136")
        self._session.headers.update(self._api_headers)
        self._session.cookies.update(cookies.to_dict())

    def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Grok API."""
        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = self._session.request(method, url, json=json_data)
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}") from e

        if response.status_code in (401, 403):
            raise GrokAuthError(
                "Request blocked (401/403). Try PlaywrightClient instead, "
                "or update ~/.grok-config.json cookies."
            )

        if response.status_code == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status_code >= 400:
            raise GrokAPIError(f"API error: {response.status_code}")

        try:
            return response.json()
        except ValueError:
            return {}

    def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL and return Content-Length."""
        try:
            response = requests.head(
                asset_url,
                headers=self._asset_headers,
                cookies=self.cookies.to_dict(),
                timeout=15,
                impersonate="chrome136",
            )
        except Exception as e:
            raise GrokAPIError(f"Asset request failed: {e}") from e

        if response.status_code == 403:
            raise GrokAuthError("Asset access denied (403). Try PlaywrightClient.")

        if response.status_code != 200:
            raise GrokAPIError(f"Asset request failed: {response.status_code}")

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header in response")

        return int(content_length)


# =============================================================================
# PlaywrightClient - Sync client using Playwright
# =============================================================================


class PlaywrightClient(SyncClientBase):
    """
    Playwright-based Grok API client (sync).

    Uses native Chromium TLS for reliable Cloudflare bypass.
    Works on all platforms. Must be used as a context manager.

    Example:
        >>> with PlaywrightClient() as client:
        ...     posts = client.list_posts(limit=10)
    """

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
    ):
        """Initialize (use as context manager to start Playwright)."""
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]

        self.cookies = cookies
        self._cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.to_dict().items()])

        browser_headers = _get_browser_headers()
        browser_headers["Cookie"] = self._cookie_str
        self._headers = browser_headers

        self._playwright: Playwright | None = None
        self._api_context: APIRequestContext | None = None
        self._asset_context: APIRequestContext | None = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        self._api_context = self._playwright.request.new_context(
            base_url=self.BASE_URL,
            extra_http_headers=self._headers,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._api_context:
            self._api_context.dispose()
        if self._asset_context:
            self._asset_context.dispose()
        if self._playwright:
            self._playwright.stop()

    def _get_asset_context(self) -> APIRequestContext:
        """Get or create asset context (lazy initialization)."""
        if self._asset_context is None:
            self._asset_context = self._playwright.request.new_context(
                extra_http_headers={
                    "Origin": "https://grok.com",
                    "Referer": "https://grok.com/",
                    "User-Agent": self._headers["User-Agent"],
                    "Cookie": self._cookie_str,
                }
            )
        return self._asset_context

    def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Grok API."""
        try:
            if method.upper() == "POST":
                response = self._api_context.post(endpoint, data=json_data)
            elif method.upper() == "GET":
                response = self._api_context.get(endpoint)
            else:
                raise GrokAPIError(f"Unsupported HTTP method: {method}")
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}") from e

        if response.status in (401, 403):
            text = response.text()
            if "Just a moment" in text:
                raise GrokAuthError("Cloudflare challenge detected. Refresh cf_clearance cookie.")
            raise GrokAuthError("Request blocked (401/403). Check cookies.")

        if response.status == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status >= 400:
            raise GrokAPIError(f"API error: {response.status}")

        try:
            return response.json()
        except ValueError:
            return {}

    def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL and return Content-Length."""
        try:
            context = self._get_asset_context()
            response = context.head(asset_url)
        except Exception as e:
            raise GrokAPIError(f"Asset request failed: {e}") from e

        if response.status == 403:
            raise GrokAuthError("Asset access denied (403). Refresh cf_clearance.")

        if response.status != 200:
            raise GrokAPIError(f"Asset request failed: {response.status}")

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header in response")

        return int(content_length)


# =============================================================================
# AsyncClient - Async client using Playwright
# =============================================================================


class AsyncClient(ResponseParser):
    """
    Async Playwright-based Grok API client.

    For use in async contexts (MCP servers, asyncio apps).
    Must be used as an async context manager.

    Example:
        >>> async with AsyncClient() as client:
        ...     posts = await client.list_posts(limit=10)
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
    ):
        """Initialize (use as async context manager to start Playwright)."""
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]

        self.cookies = cookies
        self._cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.to_dict().items()])

        browser_headers = _get_browser_headers()
        browser_headers["Cookie"] = self._cookie_str
        self._headers = browser_headers

        self._playwright: AsyncPlaywright | None = None
        self._api_context: AsyncAPIRequestContext | None = None
        self._asset_context: AsyncAPIRequestContext | None = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._api_context = await self._playwright.request.new_context(
            base_url=self.BASE_URL,
            extra_http_headers=self._headers,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._api_context:
            await self._api_context.dispose()
        if self._asset_context:
            await self._asset_context.dispose()
        if self._playwright:
            await self._playwright.stop()

    async def _get_asset_context(self) -> AsyncAPIRequestContext:
        """Get or create asset context (lazy initialization)."""
        if self._asset_context is None:
            self._asset_context = await self._playwright.request.new_context(
                extra_http_headers={
                    "Origin": "https://grok.com",
                    "Referer": "https://grok.com/",
                    "User-Agent": self._headers["User-Agent"],
                    "Cookie": self._cookie_str,
                }
            )
        return self._asset_context

    # =========================================================================
    # Async API methods
    # =========================================================================

    async def list_posts(
        self,
        limit: int = 40,
        source: str = "MEDIA_POST_SOURCE_LIKED",
    ) -> list[PostSummary]:
        """List posts. Default: your liked posts. Use source=None for all public."""
        json_data: dict[str, Any] = {"limit": limit}
        if source:
            json_data["filter"] = {"source": source}
        else:
            json_data["filter"] = {}

        data = await self._api_request("POST", "/rest/media/post/list", json_data)

        posts = []
        for item in data.get("posts", []):
            try:
                summary = self._parse_post_summary(item)
                posts.append(summary)
            except Exception:
                continue

        return posts

    async def get_post_details(self, post_id: str) -> PostDetails:
        """Get full details of a post including all child videos."""
        data = await self._api_request("POST", "/rest/media/post/get", {"id": post_id})
        post_data = data.get("post", data)
        return self._parse_post_details(post_data, post_id, raw_data=data)

    async def get_asset_file_size(self, asset_url: str) -> int:
        """Get file size of a Grok asset via HEAD request."""
        if not asset_url:
            raise GrokAPIError("Asset URL is empty")

        if not asset_url.startswith(self.ASSETS_URL):
            raise GrokAPIError(f"Invalid asset URL: {asset_url[:50]}...")

        try:
            context = await self._get_asset_context()
            response = await context.head(asset_url)
        except Exception as e:
            raise GrokAPIError(f"Asset request failed: {e}") from e

        if response.status == 403:
            raise GrokAuthError("Asset access denied (403).")

        if response.status != 200:
            raise GrokAPIError(f"Asset request failed: {response.status}")

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header")

        return int(content_length)

    async def validate_auth(self) -> bool:
        """Check if current authentication is working."""
        try:
            await self._api_request("POST", "/rest/media/post/list", {"limit": 1, "filter": {}})
            return True
        except (GrokAuthError, GrokAPIError):
            return False

    async def match_local_video(self, local_path: str | Path) -> VideoMatchResult:
        """Match a local grok video to its web counterpart."""
        local_path = Path(local_path)

        if not local_path.exists():
            raise GrokAPIError(f"File not found: {local_path}")

        filename = local_path.name
        match = re.match(
            r"grok-video-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            filename,
        )
        if not match:
            raise GrokAPIError(f"Invalid filename format: {filename}")

        parent_id = match.group(1)
        local_size = local_path.stat().st_size

        details = await self.get_post_details(parent_id)

        videos_to_check = []

        if details.mode == GenerationMode.TEXT_TO_VIDEO and details.hd_media_url:
            videos_to_check.append(
                {
                    "video_id": details.id,
                    "url": details.hd_media_url,
                    "is_parent": True,
                    "prompt": details.original_prompt,
                }
            )

        for child in details.children:
            url = child.hd_media_url or child.media_url
            if url:
                videos_to_check.append(
                    {
                        "video_id": child.id,
                        "url": url,
                        "is_parent": False,
                        "prompt": child.original_prompt,
                    }
                )

        for video in videos_to_check:
            try:
                web_size = await self.get_asset_file_size(video["url"])
                if web_size == local_size:
                    new_filename = f"grok-video_{parent_id}_{video['video_id']}.mp4"
                    return VideoMatchResult(
                        parent_id=parent_id,
                        video_id=video["video_id"],
                        is_parent_video=video["is_parent"],
                        mode=details.mode,
                        original_prompt=video["prompt"],
                        file_size=local_size,
                        new_filename=new_filename,
                    )
            except Exception:
                continue

        raise GrokAPIError(f"No matching video found for: {filename}")

    async def like_post(self, post_id: str) -> bool:
        """Like a post to save it to favorites."""
        await self._api_request("POST", "/rest/media/post/like", {"id": post_id})
        return True

    async def unlike_post(self, post_id: str) -> bool:
        """Unlike a post to remove it from favorites."""
        await self._api_request("POST", "/rest/media/post/unlike", {"id": post_id})
        return True

    async def create_video_from_image(
        self,
        image_url: str,
        parent_post_id: str,
        aspect_ratio: str = "2:3",
        video_length: int = 6,
    ) -> dict[str, Any]:
        """Generate a video from an image using Grok's chat API."""
        message = f"{image_url}  --mode=normal"

        payload = {
            "temporary": True,
            "modelName": "grok-3",
            "message": message,
            "toolOverrides": {"videoGen": True},
            "responseMetadata": {
                "experiments": [],
                "modelConfigOverride": {
                    "modelMap": {
                        "videoGenModelConfig": {
                            "parentPostId": parent_post_id,
                            "aspectRatio": aspect_ratio,
                            "videoLength": video_length,
                        }
                    }
                },
            },
        }

        return await self._api_request("POST", "/rest/app-chat/conversations/new", payload)

    # =========================================================================
    # Internal
    # =========================================================================

    async def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Grok API."""
        try:
            if method.upper() == "POST":
                response = await self._api_context.post(endpoint, data=json_data)
            elif method.upper() == "GET":
                response = await self._api_context.get(endpoint)
            else:
                raise GrokAPIError(f"Unsupported HTTP method: {method}")
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}") from e

        if response.status in (401, 403):
            text = await response.text()
            if "Just a moment" in text:
                raise GrokAuthError("Cloudflare challenge. Refresh cf_clearance.")
            raise GrokAuthError("Request blocked (401/403). Check cookies.")

        if response.status == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status >= 400:
            raise GrokAPIError(f"API error: {response.status}")

        try:
            return await response.json()
        except ValueError:
            return {}
