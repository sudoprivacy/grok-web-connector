"""Main client for Grok Imagine web API."""

import re
from pathlib import Path
from typing import Any

import requests

from .auth import load_cookies
from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from .models import GrokCookies, GrokPost, GrokVideo


class GrokClient:
    """Client for interacting with Grok Imagine web API."""

    BASE_URL = "https://grok.com"
    DEFAULT_HEADERS = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
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
        """
        if cookies is None:
            cookies = load_cookies(config_path)

        self.cookies = cookies
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)
        self.session.cookies.update(cookies.to_cookie_dict())

    def _request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """
        Make authenticated request to Grok API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (e.g., "/rest/media/post/get")
            json_data: JSON body for POST requests
            **kwargs: Additional arguments for requests

        Returns:
            JSON response as dictionary

        Raises:
            GrokAuthError: If authentication fails
            GrokNotFoundError: If resource not found
            GrokAPIError: If API request fails
        """
        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = self.session.request(
                method,
                url,
                json=json_data,
                **kwargs,
            )
        except requests.RequestException as e:
            raise GrokAPIError(f"Request failed: {e}")

        if response.status_code == 401 or response.status_code == 403:
            raise GrokAuthError(
                "Authentication failed. Your cookies may have expired.\n"
                "Please re-extract cookies from your browser and update ~/.grok-config.json"
            )

        if response.status_code == 404:
            raise GrokNotFoundError("Resource not found", status_code=404)

        if response.status_code >= 400:
            raise GrokAPIError(
                f"API error: {response.status_code} - {response.text}",
                status_code=response.status_code,
            )

        try:
            return response.json()
        except ValueError:
            # Some endpoints may return empty response
            return {}

    def get_post(self, post_id: str) -> GrokPost:
        """
        Get post details by UUID.

        Args:
            post_id: Post UUID (e.g., "0c5c5864-fadb-440b-a52b-e441dab973d3")

        Returns:
            GrokPost instance with post details

        Raises:
            GrokAuthError: If authentication fails
            GrokNotFoundError: If post not found
            GrokAPIError: If API request fails
        """
        data = self._request(
            "POST",
            "/rest/media/post/get",
            json_data={"id": post_id},
        )

        return self._parse_post(data, post_id)

    def list_posts(
        self,
        limit: int = 40,
        source: str = "MEDIA_POST_SOURCE_LIKED",
    ) -> list[GrokPost]:
        """
        List user's posts.

        Args:
            limit: Maximum number of posts to return
            source: Filter by source type

        Returns:
            List of GrokPost instances
        """
        data = self._request(
            "POST",
            "/rest/media/post/list",
            json_data={
                "limit": limit,
                "filter": {"source": source},
            },
        )

        posts = []
        for item in data.get("posts", []):
            try:
                post = self._parse_post(item, item.get("id", ""))
                posts.append(post)
            except Exception:
                # Skip posts that fail to parse
                continue

        return posts

    def _parse_post(self, data: dict[str, Any], post_id: str) -> GrokPost:
        """Parse API response into GrokPost model."""
        # Handle nested 'post' key from get_post endpoint
        if "post" in data:
            post_data = data["post"]
        else:
            post_data = data

        # Parse child posts (videos)
        videos = []
        for child in post_data.get("childPosts", []):
            if child.get("mediaType") == "MEDIA_POST_TYPE_VIDEO":
                video = GrokVideo(
                    id=child.get("id", ""),
                    original_post_id=child.get("originalPostId", post_id),
                    prompt=child.get("originalPrompt") or child.get("prompt"),
                    media_url=child.get("mediaUrl"),
                    hd_media_url=child.get("hdMediaUrl"),
                    thumbnail_url=child.get("thumbnailImageUrl"),
                    created_at=child.get("createTime"),
                    duration=child.get("videoDuration"),
                    model_name=child.get("modelName"),
                    resolution=child.get("resolution"),
                )
                videos.append(video)

        return GrokPost(
            id=post_data.get("id", post_id),
            user_id=post_data.get("userId"),
            prompt=post_data.get("prompt") or post_data.get("originalPrompt"),
            media_type=post_data.get("mediaType"),
            media_url=post_data.get("mediaUrl"),
            thumbnail_url=post_data.get("thumbnailImageUrl"),
            created_at=post_data.get("createTime"),
            model_name=post_data.get("modelName"),
            resolution=post_data.get("resolution"),
            videos=videos,
            raw_data=data,
        )

    @staticmethod
    def extract_uuid_from_filename(filename: str) -> str | None:
        """
        Extract UUID from grok video filename.

        Args:
            filename: Filename like "grok-video-{uuid}.mp4" or "grok-video-{uuid} (1).mp4"

        Returns:
            UUID string or None if not found
        """
        pattern = r"grok-video-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        match = re.search(pattern, filename, re.IGNORECASE)
        return match.group(1) if match else None

    @staticmethod
    def construct_url_from_uuid(uuid: str) -> str:
        """
        Construct Grok web URL from UUID.

        Args:
            uuid: Post UUID

        Returns:
            Full URL like "https://grok.com/imagine/post/{uuid}"
        """
        return f"https://grok.com/imagine/post/{uuid}"

    def get_post_from_filename(self, filename: str) -> GrokPost | None:
        """
        Get post details from a local video filename.

        Args:
            filename: Local video filename like "grok-video-{uuid}.mp4"

        Returns:
            GrokPost instance or None if UUID cannot be extracted
        """
        uuid = self.extract_uuid_from_filename(filename)
        if uuid is None:
            return None
        return self.get_post(uuid)
