"""
Grok Web Connector - Playwright Client

Alternative client using Playwright's APIRequestContext instead of curl_cffi.
This client uses Chromium's native TLS stack, making it compatible with
cf_clearance cookies obtained from any Chrome browser.

Use this client when curl_cffi fails due to TLS fingerprint mismatch on Windows.

Why Playwright works when curl_cffi doesn't:
    - curl_cffi impersonates Chrome's TLS fingerprint but isn't perfect
    - cf_clearance cookie binds to the TLS fingerprint of the browser that obtained it
    - Playwright uses actual Chromium networking stack = same TLS fingerprint
    - Result: cf_clearance from Chrome works with Playwright's requests

Performance note:
    Playwright's APIRequestContext is lightweight - it doesn't launch a browser.
    It only uses the networking stack, making it suitable for API calls.

Two implementations:
    - GrokPlaywrightClient: Sync API (for standalone scripts)
    - GrokAsyncPlaywrightClient: Async API (for MCP servers and async contexts)
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Playwright, APIRequestContext
from playwright.async_api import async_playwright, Playwright as AsyncPlaywright, APIRequestContext as AsyncAPIRequestContext

from .auth import get_platform_headers, load_config, load_cookies, DEFAULT_CHROME_VERSION
from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
import os
import platform
import re

from .models import (
    ChildVideo,
    GenerationMode,
    GrokCookies,
    PostDetails,
    PostSummary,
    VideoMatchResult,
)


def get_browser_headers() -> dict[str, str]:
    """
    Generate browser-like headers for API requests.
    Uses real Chrome version (143) since Playwright uses actual Chromium stack.
    """
    # For Playwright, we use the actual Chrome version since it has native TLS
    chrome_version = "143"

    system = platform.system()
    if system == "Windows":
        ua_platform = '"Windows"'
        user_agent = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
        )
    elif system == "Darwin":
        ua_platform = '"macOS"'
        user_agent = (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
        )
    else:
        ua_platform = '"Linux"'
        user_agent = (
            f"Mozilla/5.0 (X11; Linux x86_64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
        )

    return {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/imagine",
        "User-Agent": user_agent,
        "sec-ch-ua": f'"Google Chrome";v="{chrome_version}", "Chromium";v="{chrome_version}", "Not A(Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": ua_platform,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


class GrokPlaywrightClient:
    """
    Client for Grok Imagine web API using Playwright.

    Uses Playwright's APIRequestContext which provides native Chromium TLS,
    making it compatible with cf_clearance cookies from any Chrome browser.

    This is the recommended client for Windows where curl_cffi has TLS issues.

    Example:
        >>> client = GrokPlaywrightClient()
        >>> posts = client.list_posts(limit=10)
        >>> details = client.get_post_details(posts[0].id)
        >>> client.close()  # Important: clean up resources

    Or use as context manager:
        >>> with GrokPlaywrightClient() as client:
        ...     posts = client.list_posts(limit=10)
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
    ):
        """
        Initialize Playwright-based Grok client.

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Only used if cookies is None.
                        Defaults to ~/.grok-config.json
        """
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]

        self.cookies = cookies

        # Build cookie header string
        self._cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.to_dict().items()])

        # Build headers
        browser_headers = get_browser_headers()
        browser_headers["Cookie"] = self._cookie_str

        # Start Playwright
        self._playwright: Playwright = sync_playwright().start()

        # Create API context for grok.com
        self._api_context: APIRequestContext = self._playwright.request.new_context(
            base_url=self.BASE_URL,
            extra_http_headers=browser_headers,
        )

        # Separate context for assets (different headers)
        self._asset_context: APIRequestContext | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """Clean up Playwright resources."""
        if self._api_context:
            self._api_context.dispose()
            self._api_context = None
        if self._asset_context:
            self._asset_context.dispose()
            self._asset_context = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def _get_asset_context(self) -> APIRequestContext:
        """Get or create asset context (lazy initialization)."""
        if self._asset_context is None:
            self._asset_context = self._playwright.request.new_context(
                extra_http_headers={
                    "Origin": "https://grok.com",
                    "Referer": "https://grok.com/",
                    "User-Agent": get_browser_headers()["User-Agent"],
                }
            )
        return self._asset_context

    # =========================================================================
    # API 1: list_posts() - Scan and get overview
    # =========================================================================

    def list_posts(
        self,
        limit: int = 40,
        source: str | None = None,
    ) -> list[PostSummary]:
        """
        List user's posts with basic metadata.

        Args:
            limit: Maximum number of posts to return (default: 40)
            source: Filter by source type. Options:
                    - None: All posts (default)
                    - "MEDIA_POST_SOURCE_LIKED": Liked posts only

        Returns:
            List of PostSummary objects
        """
        json_data: dict[str, Any] = {"limit": limit}
        if source:
            json_data["filter"] = {"source": source}
        else:
            json_data["filter"] = {}

        data = self._api_request("POST", "/rest/media/post/list", json_data)

        posts = []
        for item in data.get("posts", []):
            try:
                summary = self._parse_post_summary(item)
                posts.append(summary)
            except Exception:
                continue

        return posts

    # =========================================================================
    # API 2: get_post_details() - Explore single post
    # =========================================================================

    def get_post_details(self, post_id: str) -> PostDetails:
        """
        Get full details of a post including all child videos.

        Args:
            post_id: Post UUID

        Returns:
            PostDetails object with all metadata and children

        Raises:
            GrokNotFoundError: If post doesn't exist
            GrokAuthError: If authentication fails
        """
        data = self._api_request("POST", "/rest/media/post/get", {"id": post_id})
        post_data = data.get("post", data)
        return self._parse_post_details(post_data, post_id, raw_data=data)

    # =========================================================================
    # API 3: get_asset_file_size() - Get file size from assets URL
    # =========================================================================

    def get_asset_file_size(self, asset_url: str) -> int:
        """
        Get file size of a Grok asset via HEAD request.

        Args:
            asset_url: Full URL to asset on assets.grok.com

        Returns:
            File size in bytes

        Raises:
            GrokAPIError: If request fails or URL is invalid
        """
        if not asset_url:
            raise GrokAPIError("Asset URL is empty")

        if not asset_url.startswith(self.ASSETS_URL):
            raise GrokAPIError(
                f"Invalid asset URL. Expected {self.ASSETS_URL}/..., got: {asset_url[:50]}..."
            )

        try:
            context = self._get_asset_context()
            response = context.head(asset_url)
        except Exception as e:
            raise GrokAPIError(f"Asset request failed: {e}")

        if response.status == 403:
            raise GrokAuthError(
                "Asset access denied (403). Check:\n"
                "1. Required headers: Referer and Origin must be https://grok.com\n"
                "2. Cookie expiration - cf_clearance may need refresh"
            )

        if response.status != 200:
            raise GrokAPIError(f"Asset request failed: {response.status}")

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header in response")

        return int(content_length)

    # =========================================================================
    # API 4: validate_auth() - Check authentication status
    # =========================================================================

    def validate_auth(self) -> bool:
        """
        Check if current authentication is working.

        Returns:
            True if requests succeed, False otherwise
        """
        try:
            self._api_request(
                "POST",
                "/rest/media/post/list",
                {"limit": 1, "filter": {}},
            )
            return True
        except (GrokAuthError, GrokAPIError):
            return False

    # =========================================================================
    # API 5: match_local_video() - Match local file to web video
    # =========================================================================

    def match_local_video(self, local_path: str | Path) -> VideoMatchResult:
        """
        Match a local grok video to its web counterpart.

        Args:
            local_path: Path to local video file

        Returns:
            VideoMatchResult with parent_id, video_id, mode, new_filename

        Raises:
            GrokAPIError: If file not found, invalid filename, or no match
        """
        local_path = Path(local_path)

        if not local_path.exists():
            raise GrokAPIError(f"File not found: {local_path}")

        filename = local_path.name
        match = re.match(
            r"grok-video-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            filename,
        )
        if not match:
            raise GrokAPIError(
                f"Invalid filename format. Expected 'grok-video-{{uuid}}.mp4', got: {filename}"
            )

        parent_id = match.group(1)
        local_size = local_path.stat().st_size

        details = self.get_post_details(parent_id)

        videos_to_check = []

        if details.mode == GenerationMode.TEXT_TO_VIDEO and details.hd_media_url:
            videos_to_check.append({
                "video_id": details.id,
                "url": details.hd_media_url,
                "is_parent": True,
                "prompt": details.original_prompt,
            })

        for child in details.children:
            url = child.hd_media_url or child.media_url
            if url:
                videos_to_check.append({
                    "video_id": child.id,
                    "url": url,
                    "is_parent": False,
                    "prompt": child.original_prompt,
                })

        for video in videos_to_check:
            try:
                web_size = self.get_asset_file_size(video["url"])
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

        raise GrokAPIError(
            f"No matching video found on web for local file: {filename}\n"
            f"Local size: {local_size} bytes\n"
            f"Parent ID: {parent_id}\n"
            f"Videos checked: {len(videos_to_check)}"
        )

    # =========================================================================
    # Internal methods
    # =========================================================================

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
            raise GrokAPIError(f"Request failed: {e}")

        if response.status in (401, 403):
            # Check if it's Cloudflare challenge
            text = response.text()
            if "Just a moment" in text:
                raise GrokAuthError(
                    "Cloudflare challenge detected (403). Your cf_clearance cookie may have expired.\n"
                    "Please refresh it by visiting grok.com in your browser."
                )
            raise GrokAuthError(
                "Request blocked (401/403). Check ~/.grok-config.json cookies."
            )

        if response.status == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status >= 400:
            raise GrokAPIError(f"API error: {response.status}")

        try:
            return response.json()
        except ValueError:
            return {}

    def _detect_generation_mode(self, post_data: dict) -> GenerationMode:
        """Detect generation mode from post metadata."""
        media_type = post_data.get("mediaType", "")
        prompt = post_data.get("prompt")
        mode = post_data.get("mode")

        if media_type == "MEDIA_POST_TYPE_VIDEO":
            if mode == "text":
                return GenerationMode.TEXT_TO_VIDEO
            return GenerationMode.UNKNOWN

        if media_type == "MEDIA_POST_TYPE_IMAGE":
            if prompt:
                return GenerationMode.GROK_IMAGE_TO_VIDEO
            else:
                return GenerationMode.UPLOAD_IMAGE_TO_VIDEO

        return GenerationMode.UNKNOWN

    def _parse_timestamp(self, value: Any) -> datetime | None:
        """Parse ISO timestamp string to datetime."""
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            if isinstance(value, str):
                value = value.replace("Z", "+00:00")
                return datetime.fromisoformat(value)
        except Exception:
            pass
        return None

    def _parse_post_summary(self, data: dict) -> PostSummary:
        """Parse API response into PostSummary."""
        mode = self._detect_generation_mode(data)
        prompt = data.get("prompt") or data.get("originalPrompt") or ""
        prompt_preview = prompt[:100] if prompt else None

        child_posts = data.get("childPosts", [])
        video_count = sum(
            1 for c in child_posts
            if c.get("mediaType") == "MEDIA_POST_TYPE_VIDEO"
        )

        return PostSummary(
            id=data.get("id", ""),
            mode=mode,
            prompt_preview=prompt_preview,
            video_count=video_count,
            created_at=self._parse_timestamp(data.get("createTime")),
            media_type=data.get("mediaType"),
        )

    def _parse_post_details(
        self,
        data: dict,
        post_id: str,
        raw_data: dict | None = None,
    ) -> PostDetails:
        """Parse API response into PostDetails."""
        mode = self._detect_generation_mode(data)

        children = []
        for child in data.get("childPosts", []):
            if child.get("mediaType") == "MEDIA_POST_TYPE_VIDEO":
                child_video = ChildVideo(
                    id=child.get("id", ""),
                    parent_id=child.get("originalPostId", post_id),
                    original_prompt=child.get("originalPrompt"),
                    media_url=child.get("mediaUrl"),
                    hd_media_url=child.get("hdMediaUrl"),
                    thumbnail_url=child.get("thumbnailImageUrl"),
                    created_at=self._parse_timestamp(child.get("createTime")),
                    resolution=child.get("resolution"),
                    duration=child.get("duration"),
                    model_name=child.get("modelName"),
                    mode=child.get("mode"),
                )
                children.append(child_video)

        return PostDetails(
            id=data.get("id", post_id),
            user_id=data.get("userId"),
            mode=mode,
            media_type=data.get("mediaType"),
            prompt=data.get("prompt"),
            original_prompt=data.get("originalPrompt"),
            media_url=data.get("mediaUrl"),
            hd_media_url=data.get("hdMediaUrl"),
            thumbnail_url=data.get("thumbnailImageUrl"),
            created_at=self._parse_timestamp(data.get("createTime")),
            resolution=data.get("resolution"),
            model_name=data.get("modelName"),
            children=children,
            raw_data=raw_data,
        )


# =============================================================================
# Async Playwright Client (for MCP servers and async contexts)
# =============================================================================


class GrokAsyncPlaywrightClient:
    """
    Async client for Grok Imagine web API using Playwright.

    This is the async version of GrokPlaywrightClient, designed for use in
    async contexts like MCP servers.

    Example:
        >>> async with GrokAsyncPlaywrightClient() as client:
        ...     posts = await client.list_posts(limit=10)
        ...     details = await client.get_post_details(posts[0].id)
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
    ):
        """Initialize but don't start Playwright yet (use async with or aopen())."""
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]

        self.cookies = cookies
        self._cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.to_dict().items()])

        browser_headers = get_browser_headers()
        browser_headers["Cookie"] = self._cookie_str
        self._headers = browser_headers

        self._playwright: AsyncPlaywright | None = None
        self._api_context: AsyncAPIRequestContext | None = None
        self._asset_context: AsyncAPIRequestContext | None = None

    async def aopen(self):
        """Initialize Playwright resources."""
        self._playwright = await async_playwright().start()
        self._api_context = await self._playwright.request.new_context(
            base_url=self.BASE_URL,
            extra_http_headers=self._headers,
        )
        return self

    async def aclose(self):
        """Clean up Playwright resources."""
        if self._api_context:
            await self._api_context.dispose()
            self._api_context = None
        if self._asset_context:
            await self._asset_context.dispose()
            self._asset_context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self):
        await self.aopen()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def _get_asset_context(self) -> AsyncAPIRequestContext:
        """Get or create asset context (lazy initialization)."""
        if self._asset_context is None:
            self._asset_context = await self._playwright.request.new_context(
                extra_http_headers={
                    "Origin": "https://grok.com",
                    "Referer": "https://grok.com/",
                    "User-Agent": get_browser_headers()["User-Agent"],
                }
            )
        return self._asset_context

    # =========================================================================
    # API methods (async versions)
    # =========================================================================

    async def list_posts(
        self,
        limit: int = 40,
        source: str | None = None,
    ) -> list[PostSummary]:
        """List user's posts with basic metadata."""
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
            raise GrokAPIError(
                f"Invalid asset URL. Expected {self.ASSETS_URL}/..., got: {asset_url[:50]}..."
            )

        try:
            context = await self._get_asset_context()
            response = await context.head(asset_url)
        except Exception as e:
            raise GrokAPIError(f"Asset request failed: {e}")

        if response.status == 403:
            raise GrokAuthError(
                "Asset access denied (403). Check:\n"
                "1. Required headers: Referer and Origin must be https://grok.com\n"
                "2. Cookie expiration - cf_clearance may need refresh"
            )

        if response.status != 200:
            raise GrokAPIError(f"Asset request failed: {response.status}")

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header in response")

        return int(content_length)

    async def validate_auth(self) -> bool:
        """Check if current authentication is working."""
        try:
            await self._api_request(
                "POST",
                "/rest/media/post/list",
                {"limit": 1, "filter": {}},
            )
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
            raise GrokAPIError(
                f"Invalid filename format. Expected 'grok-video-{{uuid}}.mp4', got: {filename}"
            )

        parent_id = match.group(1)
        local_size = local_path.stat().st_size

        details = await self.get_post_details(parent_id)

        videos_to_check = []

        if details.mode == GenerationMode.TEXT_TO_VIDEO and details.hd_media_url:
            videos_to_check.append({
                "video_id": details.id,
                "url": details.hd_media_url,
                "is_parent": True,
                "prompt": details.original_prompt,
            })

        for child in details.children:
            url = child.hd_media_url or child.media_url
            if url:
                videos_to_check.append({
                    "video_id": child.id,
                    "url": url,
                    "is_parent": False,
                    "prompt": child.original_prompt,
                })

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

        raise GrokAPIError(
            f"No matching video found on web for local file: {filename}\n"
            f"Local size: {local_size} bytes\n"
            f"Parent ID: {parent_id}\n"
            f"Videos checked: {len(videos_to_check)}"
        )

    # =========================================================================
    # Internal methods
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
            raise GrokAPIError(f"Request failed: {e}")

        if response.status in (401, 403):
            text = await response.text()
            if "Just a moment" in text:
                raise GrokAuthError(
                    "Cloudflare challenge detected (403). Your cf_clearance cookie may have expired.\n"
                    "Please refresh it by visiting grok.com in your browser."
                )
            raise GrokAuthError(
                "Request blocked (401/403). Check ~/.grok-config.json cookies."
            )

        if response.status == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status >= 400:
            raise GrokAPIError(f"API error: {response.status}")

        try:
            return await response.json()
        except ValueError:
            return {}

    # Reuse sync client's parsing methods (they don't need async)
    def _detect_generation_mode(self, post_data: dict) -> GenerationMode:
        """Detect generation mode from post metadata."""
        media_type = post_data.get("mediaType", "")
        prompt = post_data.get("prompt")
        mode = post_data.get("mode")

        if media_type == "MEDIA_POST_TYPE_VIDEO":
            if mode == "text":
                return GenerationMode.TEXT_TO_VIDEO
            return GenerationMode.UNKNOWN

        if media_type == "MEDIA_POST_TYPE_IMAGE":
            if prompt:
                return GenerationMode.GROK_IMAGE_TO_VIDEO
            else:
                return GenerationMode.UPLOAD_IMAGE_TO_VIDEO

        return GenerationMode.UNKNOWN

    def _parse_timestamp(self, value: Any) -> datetime | None:
        """Parse ISO timestamp string to datetime."""
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            if isinstance(value, str):
                value = value.replace("Z", "+00:00")
                return datetime.fromisoformat(value)
        except Exception:
            pass
        return None

    def _parse_post_summary(self, data: dict) -> PostSummary:
        """Parse API response into PostSummary."""
        mode = self._detect_generation_mode(data)
        prompt = data.get("prompt") or data.get("originalPrompt") or ""
        prompt_preview = prompt[:100] if prompt else None

        child_posts = data.get("childPosts", [])
        video_count = sum(
            1 for c in child_posts
            if c.get("mediaType") == "MEDIA_POST_TYPE_VIDEO"
        )

        return PostSummary(
            id=data.get("id", ""),
            mode=mode,
            prompt_preview=prompt_preview,
            video_count=video_count,
            created_at=self._parse_timestamp(data.get("createTime")),
            media_type=data.get("mediaType"),
        )

    def _parse_post_details(
        self,
        data: dict,
        post_id: str,
        raw_data: dict | None = None,
    ) -> PostDetails:
        """Parse API response into PostDetails."""
        mode = self._detect_generation_mode(data)

        children = []
        for child in data.get("childPosts", []):
            if child.get("mediaType") == "MEDIA_POST_TYPE_VIDEO":
                child_video = ChildVideo(
                    id=child.get("id", ""),
                    parent_id=child.get("originalPostId", post_id),
                    original_prompt=child.get("originalPrompt"),
                    media_url=child.get("mediaUrl"),
                    hd_media_url=child.get("hdMediaUrl"),
                    thumbnail_url=child.get("thumbnailImageUrl"),
                    created_at=self._parse_timestamp(child.get("createTime")),
                    resolution=child.get("resolution"),
                    duration=child.get("duration"),
                    model_name=child.get("modelName"),
                    mode=child.get("mode"),
                )
                children.append(child_video)

        return PostDetails(
            id=data.get("id", post_id),
            user_id=data.get("userId"),
            mode=mode,
            media_type=data.get("mediaType"),
            prompt=data.get("prompt"),
            original_prompt=data.get("originalPrompt"),
            media_url=data.get("mediaUrl"),
            hd_media_url=data.get("hdMediaUrl"),
            thumbnail_url=data.get("thumbnailImageUrl"),
            created_at=self._parse_timestamp(data.get("createTime")),
            resolution=data.get("resolution"),
            model_name=data.get("modelName"),
            children=children,
            raw_data=raw_data,
        )
