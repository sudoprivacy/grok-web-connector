"""
Grok Web Connector - Main Client

Provides 4 core APIs for interacting with Grok Imagine:
1. list_posts() - Scan and get overview of all posts
2. get_post_details() - Get full details for a specific post
3. get_asset_file_size() - Get file size from assets.grok.com URL
4. validate_auth() - Check if authentication is valid
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .auth import load_cookies
from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from .models import (
    ChildVideo,
    GenerationMode,
    GrokCookies,
    PostDetails,
    PostSummary,
)


class GrokClient:
    """
    Client for Grok Imagine web API.

    Example:
        >>> client = GrokClient()
        >>> posts = client.list_posts(limit=10)
        >>> details = client.get_post_details(posts[0].id)
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    # Headers for API requests
    API_HEADERS = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    # Headers for asset requests (requires Referer and Origin!)
    ASSET_HEADERS = {
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
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
            config_path: Path to config file. Only used if cookies is None.
                        Defaults to ~/.grok-config.json
        """
        if cookies is None:
            cookies = load_cookies(config_path)

        self.cookies = cookies
        self._session = requests.Session()
        self._session.headers.update(self.API_HEADERS)
        self._session.cookies.update(cookies.to_dict())

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
            List of PostSummary objects with:
            - id: Post UUID
            - mode: Generation mode (img2vid, txt2vid, upload2vid)
            - prompt_preview: First 100 chars of prompt
            - video_count: Number of child videos
            - created_at: Creation timestamp

        Example:
            >>> posts = client.list_posts(limit=10)
            >>> for p in posts:
            ...     print(f"{p.id}: {p.mode.value} ({p.video_count} videos)")
        """
        json_data: dict[str, Any] = {"limit": limit}
        if source:
            json_data["filter"] = {"source": source}

        data = self._api_request("POST", "/rest/media/post/list", json_data)

        posts = []
        for item in data.get("posts", []):
            try:
                summary = self._parse_post_summary(item)
                posts.append(summary)
            except Exception:
                continue  # Skip malformed posts

        return posts

    # =========================================================================
    # API 2: get_post_details() - Explore single post
    # =========================================================================

    def get_post_details(self, post_id: str) -> PostDetails:
        """
        Get full details of a post including all child videos.

        Args:
            post_id: Post UUID (from filename or web URL)

        Returns:
            PostDetails object with:
            - Parent post metadata (prompt, media_url, etc.)
            - Generation mode detection
            - All child videos with their metadata

        Raises:
            GrokNotFoundError: If post doesn't exist
            GrokAuthError: If authentication fails

        Example:
            >>> details = client.get_post_details("0c5c5864-fadb-440b-a52b-e441dab973d3")
            >>> print(f"Mode: {details.mode.value}")
            >>> print(f"Children: {details.video_count}")
            >>> for child in details.children:
            ...     print(f"  - {child.id}: {child.original_prompt[:50]}")
        """
        data = self._api_request("POST", "/rest/media/post/get", {"id": post_id})

        # Handle nested 'post' key
        post_data = data.get("post", data)

        return self._parse_post_details(post_data, post_id, raw_data=data)

    # =========================================================================
    # API 3: get_asset_file_size() - Get file size from assets URL
    # =========================================================================

    def get_asset_file_size(self, asset_url: str) -> int:
        """
        Get file size of a Grok asset (video or image) via HEAD request.

        This method handles the special headers required by assets.grok.com:
        - Referer: https://grok.com/
        - Origin: https://grok.com

        Without these headers, requests return 403 Forbidden.

        Args:
            asset_url: Full URL to asset on assets.grok.com
                      (e.g., from ChildVideo.hd_media_url)

        Returns:
            File size in bytes

        Raises:
            GrokAPIError: If request fails or URL is invalid

        Example:
            >>> details = client.get_post_details(post_id)
            >>> for child in details.children:
            ...     if child.hd_media_url:
            ...         size = client.get_asset_file_size(child.hd_media_url)
            ...         print(f"{child.id}: {size} bytes")
        """
        if not asset_url:
            raise GrokAPIError("Asset URL is empty")

        if not asset_url.startswith(self.ASSETS_URL):
            raise GrokAPIError(
                f"Invalid asset URL. Expected {self.ASSETS_URL}/..., got: {asset_url[:50]}..."
            )

        try:
            response = requests.head(
                asset_url,
                headers=self.ASSET_HEADERS,
                cookies=self.cookies.to_dict(),
                timeout=15,
            )
        except requests.RequestException as e:
            raise GrokAPIError(f"Asset request failed: {e}")

        if response.status_code == 403:
            raise GrokAuthError(
                "Asset access denied (403). Cookies may have expired."
            )

        if response.status_code != 200:
            raise GrokAPIError(
                f"Asset request failed: {response.status_code}"
            )

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header in response")

        return int(content_length)

    # =========================================================================
    # API 4: validate_auth() - Check authentication status
    # =========================================================================

    def validate_auth(self) -> bool:
        """
        Check if current authentication cookies are valid.

        Returns:
            True if authentication is valid, False otherwise

        Example:
            >>> if not client.validate_auth():
            ...     print("Please update your cookies!")
        """
        try:
            # Try to list posts with minimal limit
            self._api_request(
                "POST",
                "/rest/media/post/list",
                {"limit": 1},
            )
            return True
        except GrokAuthError:
            return False
        except Exception:
            return False

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
        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = self._session.request(method, url, json=json_data)
        except requests.RequestException as e:
            raise GrokAPIError(f"Request failed: {e}")

        if response.status_code in (401, 403):
            raise GrokAuthError(
                "Authentication failed. Cookies may have expired.\n"
                "Please update ~/.grok-config.json with fresh cookies."
            )

        if response.status_code == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status_code >= 400:
            raise GrokAPIError(f"API error: {response.status_code}")

        try:
            return response.json()
        except ValueError:
            return {}

    def _detect_generation_mode(self, post_data: dict) -> GenerationMode:
        """
        Detect generation mode from post metadata.

        Logic:
        - MEDIA_POST_TYPE_VIDEO + mode=text → TEXT_TO_VIDEO
        - MEDIA_POST_TYPE_IMAGE + prompt exists → GROK_IMAGE_TO_VIDEO
        - MEDIA_POST_TYPE_IMAGE + no prompt → UPLOAD_IMAGE_TO_VIDEO
        """
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
            # Handle 'Z' suffix
            if isinstance(value, str):
                value = value.replace("Z", "+00:00")
                return datetime.fromisoformat(value)
        except Exception:
            pass
        return None

    def _parse_post_summary(self, data: dict) -> PostSummary:
        """Parse API response into PostSummary."""
        mode = self._detect_generation_mode(data)

        # Get prompt for preview
        prompt = data.get("prompt") or data.get("originalPrompt") or ""
        prompt_preview = prompt[:100] if prompt else None

        # Count child videos
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
        """Parse API response into PostDetails with all children."""
        mode = self._detect_generation_mode(data)

        # Parse child videos
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
