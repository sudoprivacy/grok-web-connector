"""
Internal implementation for Grok Web Connector.

This module contains base classes that are not part of the public API:
- ResponseParser: Parses API responses into Python objects
- SyncClientBase: Base class for synchronous clients

Do not import from this module directly. Use the public API from grok_web instead.
"""

import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from .exceptions import GrokAPIError, GrokAuthError
from .models import (
    ChildVideo,
    GenerationMode,
    PostDetails,
    PostSummary,
    VideoGenerationResult,
    VideoMatchResult,
)


class ResponseParser:
    """
    Parses Grok API responses into Python objects.

    This class handles all data transformation from raw JSON to domain models.
    Used by both sync and async clients.
    """

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

    def _parse_post_summary(self, data: dict, include_raw_data: bool = False) -> PostSummary:
        """Parse API response into PostSummary."""
        mode = self._detect_generation_mode(data)

        prompt = data.get("prompt") or data.get("originalPrompt") or ""
        prompt_preview = prompt[:100] if prompt else None

        child_posts = data.get("childPosts", [])
        video_count = sum(1 for c in child_posts if c.get("mediaType") == "MEDIA_POST_TYPE_VIDEO")

        return PostSummary(
            id=data.get("id", ""),
            mode=mode,
            prompt_preview=prompt_preview,
            video_count=video_count,
            created_at=self._parse_timestamp(data.get("createTime")),
            media_type=data.get("mediaType"),
            raw_data=data if include_raw_data else None,
        )

    def _parse_post_details(
        self,
        data: dict,
        post_id: str,
        raw_data: dict | None = None,
    ) -> PostDetails:
        """Parse API response into PostDetails with all children."""
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


class SyncClientBase(ResponseParser, ABC):
    """
    Abstract base class for synchronous Grok API clients.

    Implements all API methods. Subclasses only need to provide:
    - __init__(): Initialize HTTP client
    - _api_request(): Make authenticated API requests
    - _asset_request_head(): Make HEAD request to assets URL
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    # =========================================================================
    # Abstract methods (must be implemented by subclasses)
    # =========================================================================

    @abstractmethod
    def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Grok API."""
        pass

    @abstractmethod
    def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL and return Content-Length."""
        pass

    # =========================================================================
    # API 1: list_posts()
    # =========================================================================

    def list_posts(
        self,
        limit: int = 40,
        source: str = "MEDIA_POST_SOURCE_LIKED",
        include_raw_data: bool = False,
    ) -> list[PostSummary]:
        """
        List posts with basic metadata.

        By default, returns only your liked/favorited posts (the only reliable
        way to access your own content). Use source=None to browse all public
        posts from any user.

        Args:
            limit: Maximum number of posts to return (default: 40)
            source: Filter by source type. Options:
                    - "MEDIA_POST_SOURCE_LIKED": Your liked posts only (default)
                    - None: All public posts (from any user, not just yours)
            include_raw_data: If True, include raw API response in each PostSummary.
                              Default False for better performance.

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
                summary = self._parse_post_summary(item, include_raw_data=include_raw_data)
                posts.append(summary)
            except Exception:
                continue

        return posts

    # =========================================================================
    # API 2: get_post_details()
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
    # API 3: get_asset_file_size()
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

        return self._asset_request_head(asset_url)

    # =========================================================================
    # API 4: validate_auth()
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
    # API 5: match_local_video()
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
    # API 6: like_post()
    # =========================================================================

    def like_post(self, post_id: str) -> bool:
        """
        Like a post to save it to favorites.

        This is the ONLY way to keep posts accessible long-term in Grok Imagine.

        Args:
            post_id: Post UUID to like

        Returns:
            True if successful
        """
        self._api_request("POST", "/rest/media/post/like", {"id": post_id})
        return True

    # =========================================================================
    # API 7: unlike_post()
    # =========================================================================

    def unlike_post(self, post_id: str) -> bool:
        """
        Unlike a post to remove it from favorites.

        WARNING: This effectively deletes the post from your view.

        Args:
            post_id: Post UUID to unlike

        Returns:
            True if successful
        """
        self._api_request("POST", "/rest/media/post/unlike", {"id": post_id})
        return True

    # =========================================================================
    # API 8: create_video_from_image()
    # =========================================================================

    def create_video_from_image(
        self,
        image_url: str,
        parent_post_id: str,
        aspect_ratio: str = "2:3",
        video_length: int = 6,
        statsig_id: str | None = None,
    ) -> VideoGenerationResult:
        """
        Generate a video from an image using Grok's chat API.

        This method blocks until video generation is complete (typically 20-30 seconds).
        The API returns a streaming NDJSON response with progress updates.

        Args:
            image_url: Full URL to image on imagine-public.x.ai
            parent_post_id: Parent image post UUID
            aspect_ratio: Video aspect ratio (default: "2:3")
            video_length: Duration in seconds (default: 6)
            statsig_id: Optional x-statsig-id for style control.
                       Same ID produces similar video styles (camera motion, etc.).
                       If None, generates a new random ID (explores new styles).

        Returns:
            VideoGenerationResult with video_id, moderated status, statsig_id used, etc.

        Raises:
            GrokAPIError: If generation fails or response cannot be parsed
            GrokAuthError: If authentication fails (403)
        """
        import base64
        import json
        import os

        # Generate or use provided statsig_id
        if statsig_id is None:
            # Generate new random 70-byte ID for style exploration
            statsig_id = base64.b64encode(os.urandom(70)).decode("utf-8").rstrip("=")

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

        # Pass statsig_id as extra header
        extra_headers = {"x-statsig-id": statsig_id}

        # Get raw text response (NDJSON streaming format)
        response_text = self._api_request_text(
            "POST", "/rest/app-chat/conversations/new", payload, extra_headers=extra_headers
        )

        # Parse NDJSON response - each line is a JSON object
        conversation_id = None
        video_result = None

        for line in response_text.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                result = data.get("result", {})

                # Extract conversation ID from first response
                if "conversation" in result:
                    conversation_id = result["conversation"].get("conversationId")

                # Extract video generation result
                response = result.get("response", {})
                if "streamingVideoGenerationResponse" in response:
                    video_data = response["streamingVideoGenerationResponse"]
                    # Keep updating with latest progress
                    video_result = video_data

            except json.JSONDecodeError:
                continue

        if not video_result:
            raise GrokAPIError(
                "Failed to parse video generation response. "
                "No streamingVideoGenerationResponse found."
            )

        return VideoGenerationResult(
            video_id=video_result.get("videoId", ""),
            parent_post_id=video_result.get("parentPostId", parent_post_id),
            moderated=video_result.get("moderated", False),
            progress=video_result.get("progress", 0),
            mode=video_result.get("mode", "normal"),
            model_name=video_result.get("modelName"),
            image_reference=video_result.get("imageReference"),
            conversation_id=conversation_id,
            statsig_id=statsig_id,
        )

    @abstractmethod
    def _api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Make authenticated request and return raw text response.

        Args:
            extra_headers: Additional headers to include (e.g., x-statsig-id)
        """
        pass
