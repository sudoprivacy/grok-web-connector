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

from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError, GrokRateLimitError
from .models import (
    ChildVideo,
    GenerationMode,
    PostDetails,
    PostSummary,
    VideoGenerationResult,
    VideoMatchResult,
    VideoPreset,
)

# =============================================================================
# Shared Utilities
# =============================================================================

# Media post API endpoints
MEDIA_POST_LIKE_ENDPOINT = "/rest/media/post/like"
MEDIA_POST_UNLIKE_ENDPOINT = "/rest/media/post/unlike"
MEDIA_POST_LIST_ENDPOINT = "/rest/media/post/list"
MEDIA_POST_GET_ENDPOINT = "/rest/media/post/get"

PRESET_MAP = {
    "normal": "normal",
    "fun": "extremely-crazy",
    "spicy": "extremely-spicy-or-crazy",
}


def resolve_preset(preset: VideoPreset | str) -> str:
    """Resolve preset name to API mode value."""
    if isinstance(preset, VideoPreset):
        return preset.value
    elif isinstance(preset, str) and preset.lower() in PRESET_MAP:
        return PRESET_MAP[preset.lower()]
    return str(preset)


def generate_statsig_id() -> str:
    """Generate a random statsig_id for video style exploration."""
    import base64
    import os

    return base64.b64encode(os.urandom(70)).decode("utf-8").rstrip("=")


def build_video_payload(
    image_url: str,
    parent_post_id: str,
    mode_value: str,
    aspect_ratio: str = "2:3",
    video_length: int = 6,
    adjustment_prompt: str | None = None,
) -> dict:
    """Build the payload for video generation API.

    Args:
        image_url: Source image URL
        parent_post_id: Parent post UUID
        mode_value: Video mode (normal, extremely-crazy, etc.)
        aspect_ratio: Video aspect ratio
        video_length: Video duration in seconds
        adjustment_prompt: Optional prompt to guide video generation (e.g., camera movement,
            character actions). If provided, mode is automatically set to 'custom'.
            Examples: "camera slowly zooms out", "she turns her head to the left",
            "static camera, no zoom"
    """
    # If adjustment_prompt is provided, use custom mode
    if adjustment_prompt:
        message = f"{image_url} {adjustment_prompt} --mode=custom"
    else:
        message = f"{image_url}  --mode={mode_value}"
    return {
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


def parse_video_ndjson_response(
    response_text: str,
    parent_post_id: str,
    statsig_id: str,
) -> VideoGenerationResult:
    """Parse NDJSON response from video generation API.

    Raises:
        GrokRateLimitError: When API returns "Too many requests" (error code 8).
            This is a GLOBAL rate limit - stop all requests and wait.
            As of December 2025, rate limits reset every hour.
        GrokAPIError: For other parsing failures.
    """
    import json

    conversation_id = None
    video_result = None

    for line in response_text.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)

            # Check for rate limit error (code 8: "Too many requests")
            if "error" in data:
                error = data["error"]
                error_code = error.get("code")
                error_message = error.get("message", "Unknown error")

                if error_code == 8 or "too many requests" in error_message.lower():
                    raise GrokRateLimitError(
                        f"Rate limit exceeded: {error_message}. "
                        "This is a GLOBAL limit - stop all requests and wait. "
                        "Rate limits reset every hour (as of December 2025)."
                    )

            result = data.get("result", {})

            if "conversation" in result:
                conversation_id = result["conversation"].get("conversationId")

            response = result.get("response", {})
            if "streamingVideoGenerationResponse" in response:
                video_result = response["streamingVideoGenerationResponse"]
        except json.JSONDecodeError:
            continue

    if not video_result:
        # Include response preview for debugging
        preview = response_text[:500] if response_text else "(empty)"
        raise GrokAPIError(
            "Failed to parse video generation response. "
            f"No streamingVideoGenerationResponse found. Response preview: {preview}"
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


# =============================================================================
# Bridge Pattern: Separate I/O operations from business logic
# =============================================================================


class IOBridge(ABC):
    """
    Bridge interface for I/O operations (Implementor in Bridge Pattern).

    This interface abstracts sync vs async I/O execution, allowing business
    logic to be written once and reused by both sync and async clients.
    """

    @abstractmethod
    def execute_api_request(self, method: str, endpoint: str, json_data: dict | None = None) -> Any:
        """Execute API request - implementation handles sync/async."""
        pass

    @abstractmethod
    def execute_api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> Any:
        """Execute API request returning raw text - implementation handles sync/async."""
        pass

    @abstractmethod
    def execute_asset_request_head(self, asset_url: str) -> Any:
        """Execute HEAD request to asset URL - implementation handles sync/async."""
        pass


class SyncIOBridge(IOBridge):
    """Concrete implementation for synchronous I/O operations."""

    def __init__(self, client: "SyncClientBase"):
        self._client = client

    def execute_api_request(self, method: str, endpoint: str, json_data: dict | None = None) -> Any:
        """Execute sync API request."""
        return self._client._api_request(method, endpoint, json_data)

    def execute_api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Execute sync API request returning raw text."""
        return self._client._api_request_text(method, endpoint, json_data, extra_headers)

    def execute_asset_request_head(self, asset_url: str) -> int:
        """Execute sync HEAD request to asset URL."""
        return self._client._asset_request_head(asset_url)


class AsyncIOBridge(IOBridge):
    """Concrete implementation for asynchronous I/O operations."""

    def __init__(self, client: "AsyncClientBase"):
        self._client = client

    async def execute_api_request(
        self, method: str, endpoint: str, json_data: dict | None = None
    ) -> Any:
        """Execute async API request."""
        return await self._client._api_request(method, endpoint, json_data)

    async def execute_api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Execute async API request returning raw text."""
        return await self._client._api_request_text(method, endpoint, json_data, extra_headers)

    async def execute_asset_request_head(self, asset_url: str) -> int:
        """Execute async HEAD request to asset URL."""
        return await self._client._asset_request_head(asset_url)


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


# =============================================================================
# Business Logic Layer (Abstraction in Bridge Pattern)
# =============================================================================


class GrokClientLogic(ResponseParser):
    """
    Pure business logic without I/O coupling (Refined Abstraction in Bridge Pattern).

    This class contains all business logic that doesn't require I/O operations.
    It works with both sync and async clients through the IOBridge interface.

    Benefits:
    - Business logic written once, reused by sync and async clients
    - Easy to test without mocking I/O
    - Clear separation of concerns
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    def validate_asset_url(self, asset_url: str) -> None:
        """
        Validate asset URL format (pure validation, no I/O).

        Args:
            asset_url: URL to validate

        Raises:
            GrokAPIError: If URL is invalid
        """
        if not asset_url:
            raise GrokAPIError("Asset URL is empty")

        # Accept both assets.grok.com and imagine-public.x.ai URLs
        if not (
            asset_url.startswith(self.ASSETS_URL)
            or asset_url.startswith("https://imagine-public.x.ai/")
        ):
            raise GrokAPIError(
                f"Invalid asset URL. Expected {self.ASSETS_URL}/... or "
                f"https://imagine-public.x.ai/..., got: {asset_url[:50]}..."
            )

    def extract_parent_info_from_details(
        self, details: PostDetails, video_id: str
    ) -> tuple[str, bool]:
        """
        Extract parent ID and parent status from PostDetails (pure logic, no I/O).

        Args:
            details: Post details containing raw API data
            video_id: Video ID being processed

        Returns:
            Tuple of (parent_id, is_parent_video)
        """
        # Extract parent_id from raw_data (originalPostId field)
        # For child videos: originalPostId is the parent's ID
        # For parent videos (txt2vid): originalPostId equals video_id or is missing
        raw_post = details.raw_data.get("post", details.raw_data) if details.raw_data else {}
        original_post_id = raw_post.get("originalPostId")

        if original_post_id and original_post_id != video_id:
            # This is a child video - originalPostId is the parent
            return original_post_id, False
        else:
            # This is a parent video (txt2vid) or originalPostId is same as id
            return video_id, True

    def verify_file_size_match(
        self, video_id: str, filename: str, local_size: int, web_size: int
    ) -> None:
        """
        Verify local and web file sizes match (pure validation, no I/O).

        Args:
            video_id: Video ID for error message
            filename: Filename for error message
            local_size: Local file size in bytes
            web_size: Web file size in bytes

        Raises:
            GrokAPIError: If sizes don't match
        """
        if web_size != local_size:
            raise GrokAPIError(
                f"File size mismatch for video: {video_id}\n"
                f"Local file: {filename}\n"
                f"Local size: {local_size}, Web size: {web_size}"
            )

    def build_video_match_result(
        self,
        parent_id: str,
        video_id: str,
        is_parent_video: bool,
        details: PostDetails,
        local_size: int,
    ) -> VideoMatchResult:
        """
        Build VideoMatchResult from components (pure construction, no I/O).

        Args:
            parent_id: Parent post ID
            video_id: Video ID
            is_parent_video: Whether this is a parent video
            details: Post details
            local_size: Local file size

        Returns:
            VideoMatchResult object
        """
        new_filename = f"grok-video_{parent_id}_{video_id}.mp4"
        return VideoMatchResult(
            parent_id=parent_id,
            video_id=video_id,
            is_parent_video=is_parent_video,
            mode=details.mode,
            original_prompt=details.original_prompt,
            file_size=local_size,
            new_filename=new_filename,
        )

    def extract_media_url_from_details(
        self, details: PostDetails, video_id: str, filename: str
    ) -> str:
        """
        Extract media URL from PostDetails (pure extraction, no I/O).

        Args:
            details: Post details containing media URLs
            video_id: Video ID for error message
            filename: Filename for error message

        Returns:
            Media URL (prefers HD)

        Raises:
            GrokAPIError: If no media URL found
        """
        url = details.hd_media_url or details.media_url
        if not url:
            raise GrokAPIError(
                f"No media URL found for video: {video_id}\n" f"Local file: {filename}"
            )
        return url


class SyncClientBase(ResponseParser, ABC):
    """
    Abstract base class for synchronous Grok API clients.

    Implements all API methods. Subclasses only need to provide:
    - __init__(): Initialize HTTP client (must call super().__init__())
    - _api_request(): Make authenticated API requests
    - _asset_request_head(): Make HEAD request to assets URL

    Uses Bridge Pattern to separate business logic from I/O operations,
    eliminating code duplication between sync and async implementations.
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    def __init__(self):
        """Initialize client with business logic layer."""
        super().__init__()
        self._logic = GrokClientLogic()

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
            asset_url: Full URL to asset on assets.grok.com or imagine-public.x.ai

        Returns:
            File size in bytes

        Raises:
            GrokAPIError: If request fails or URL is invalid
        """
        # Use business logic layer for validation
        self._logic.validate_asset_url(asset_url)
        # Execute I/O operation
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

        Supports two filename formats:
        - Old format: grok-video-{parent_uuid}.mp4 (extracts parent_id)
        - Web format: {video_uuid}.mp4 or {video_uuid}_hd.mp4 (extracts video_id)

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
        local_size = local_path.stat().st_size

        # Try old format: grok-video-{parent_uuid}.mp4
        old_match = re.match(
            r"grok-video-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            filename,
        )

        # Try web format: {video_uuid}.mp4 or {video_uuid}_hd.mp4 or {video_uuid}_hd (1).mp4
        web_match = re.match(
            r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
            r"(?:_hd)?(?:\s*\(\d+\))?\.mp4$",
            filename,
        )

        if old_match:
            # Old format: grok-video-{uuid}.mp4
            # The UUID could be: (1) parent_id, (2) video_id, or (3) unknown grok internal ID
            extracted_uuid = old_match.group(1)

            # Try 1: Treat as parent_id
            try:
                return self._match_by_parent_id(extracted_uuid, local_size, filename)
            except GrokNotFoundError:
                pass  # Parent doesn't exist, try next

            # Try 2: Treat as video_id
            try:
                return self._match_by_video_id(extracted_uuid, local_size, filename)
            except (GrokNotFoundError, GrokAPIError):
                pass  # Video doesn't exist, try fallback

            # Try 3: Fallback - search all liked posts by file size
            return self._match_by_file_size_via_favorites(
                local_size, filename, hint_uuid=extracted_uuid
            )

        elif web_match:
            # Web format: we have video_id, need to search favorites
            video_id = web_match.group(1)
            return self._match_by_video_id(video_id, local_size, filename)
        else:
            raise GrokAPIError(
                f"Invalid filename format. Expected 'grok-video-{{uuid}}.mp4' or "
                f"'{{uuid}}.mp4' or '{{uuid}}_hd.mp4', got: {filename}"
            )

    def _match_by_parent_id(
        self, parent_id: str, local_size: int, filename: str
    ) -> VideoMatchResult:
        """Match video by parent ID (old format)."""
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

    def _match_by_video_id(self, video_id: str, local_size: int, filename: str) -> VideoMatchResult:
        """Match video by video ID (web format) - O(1) direct lookup.

        Uses direct API call with video_id to get video details and parent info.
        The API returns originalPostId for child videos, enabling O(1) lookup.

        Uses Bridge Pattern: I/O operations here, business logic in GrokClientLogic.
        """
        # I/O Layer: Fetch post details
        try:
            details = self.get_post_details(video_id)
        except (GrokAuthError, GrokNotFoundError):
            raise
        except Exception as e:
            raise GrokAPIError(
                f"Failed to get video details: {video_id}\n"
                f"Local file: {filename}\n"
                f"Error: {e}"
            ) from e

        # Business Logic Layer: Extract media URL
        url = self._logic.extract_media_url_from_details(details, video_id, filename)

        # I/O Layer: Fetch file size
        web_size = self.get_asset_file_size(url)

        # Business Logic Layer: Extract parent info, verify size, build result
        parent_id, is_parent_video = self._logic.extract_parent_info_from_details(details, video_id)
        self._logic.verify_file_size_match(video_id, filename, local_size, web_size)
        return self._logic.build_video_match_result(
            parent_id, video_id, is_parent_video, details, local_size
        )

    def _match_by_file_size_via_favorites(
        self, local_size: int, filename: str, hint_uuid: str | None = None, max_posts: int = 200
    ) -> VideoMatchResult:
        """Fallback: Search all liked posts to find video by file size.

        This handles the case where the UUID in the filename doesn't match any
        known parent_id or video_id (e.g., grok.com uses internal session IDs
        for download filenames).
        """
        posts = self.list_posts(limit=max_posts, source="MEDIA_POST_SOURCE_LIKED")

        for post_summary in posts:
            try:
                details = self.get_post_details(post_summary.id)

                # Check parent video (for txt2vid posts)
                if details.mode == GenerationMode.TEXT_TO_VIDEO and details.hd_media_url:
                    try:
                        web_size = self.get_asset_file_size(details.hd_media_url)
                        if web_size == local_size:
                            return VideoMatchResult(
                                parent_id=details.id,
                                video_id=details.id,
                                is_parent_video=True,
                                mode=details.mode,
                                original_prompt=details.original_prompt,
                                file_size=local_size,
                                new_filename=f"grok-video_{details.id}_{details.id}.mp4",
                            )
                    except Exception:
                        pass

                # Check all children
                for child in details.children:
                    url = child.hd_media_url or child.media_url
                    if not url:
                        continue

                    try:
                        web_size = self.get_asset_file_size(url)
                        if web_size == local_size:
                            return VideoMatchResult(
                                parent_id=post_summary.id,
                                video_id=child.id,
                                is_parent_video=False,
                                mode=details.mode,
                                original_prompt=child.original_prompt,
                                file_size=local_size,
                                new_filename=f"grok-video_{post_summary.id}_{child.id}.mp4",
                            )
                    except Exception:
                        continue

            except Exception:
                continue

        # Not found
        hint_msg = f" (extracted UUID: {hint_uuid})" if hint_uuid else ""
        raise GrokAPIError(
            f"No matching video found by file size in recent {max_posts} favorites.\n"
            f"Local file: {filename}{hint_msg}\n"
            f"Local size: {local_size} bytes\n\n"
            f"The UUID in the filename doesn't match any known post or video ID.\n"
            f"Possible causes:\n"
            f"1. The video's parent post is not in your favorites - add it first\n"
            f"2. The video may have been deleted from grok.com\n"
            f"3. Try increasing max_posts if you have many favorites"
        )

    # =========================================================================
    # API 6: favorite_post()
    # =========================================================================

    def favorite_post(self, post_id: str) -> bool:
        """
        Add a post to favorites (save it).

        This is the ONLY way to keep posts accessible long-term in Grok Imagine.

        Args:
            post_id: Post UUID to favorite

        Returns:
            True if successful
        """
        self._api_request("POST", MEDIA_POST_LIKE_ENDPOINT, {"id": post_id})
        return True

    # =========================================================================
    # API 7: unfavorite_post()
    # =========================================================================

    def unfavorite_post(self, post_id: str) -> bool:
        """
        Remove a post from favorites.

        WARNING: This effectively removes the post from your saved collection.

        Args:
            post_id: Post UUID to unfavorite

        Returns:
            True if successful
        """
        self._api_request("POST", MEDIA_POST_UNLIKE_ENDPOINT, {"id": post_id})
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
        preset: VideoPreset | str = "normal",
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
            preset: Video generation preset (default: "normal").
                - VideoPreset.NORMAL / "normal": Standard generation
                - VideoPreset.FUN / "fun": More dynamic/creative motion
                - VideoPreset.SPICY / "spicy": Most permissive content filter
            statsig_id: Style seed for video generation (x-statsig-id header).

                **Style Control Behavior:**
                - Same statsig_id → ~99% similar video style (camera motion, character
                  movement patterns, animation timing). Videos may differ slightly in
                  fine details (e.g., ending micro-expressions) but overall motion and
                  framing will be nearly identical.
                - Different statsig_id → potentially different style. May produce
                  different camera movements (zoom vs pan), character actions
                  (static vs moving), and overall animation feel.
                - None (default) → generates random 70-byte ID, useful for exploring
                  diverse styles in MCTS-style search.

                **Format:** 94-char Base64 string encoding 70 random bytes.
                Server accepts any valid Base64 of this length.

                **Note:** statsig_id does NOT affect content moderation. Moderation
                is determined by image content, not by this ID.

                **MCTS Usage:**
                - Exploration: omit statsig_id to discover new styles
                - Exploitation: reuse statsig_id from successful generations
                - The returned VideoGenerationResult.statsig_id can be saved and
                  reused to reproduce similar styles

        Returns:
            VideoGenerationResult with video_id, moderated status, statsig_id used, etc.

        Raises:
            GrokAPIError: If generation fails or response cannot be parsed
            GrokAuthError: If authentication fails (403)
        """
        # Generate or use provided statsig_id
        if statsig_id is None:
            statsig_id = generate_statsig_id()

        # Build payload using shared utility
        mode_value = resolve_preset(preset)
        payload = build_video_payload(
            image_url, parent_post_id, mode_value, aspect_ratio, video_length
        )

        # Get raw text response (NDJSON streaming format)
        response_text = self._api_request_text(
            "POST",
            "/rest/app-chat/conversations/new",
            payload,
            extra_headers={"x-statsig-id": statsig_id},
        )

        # Parse using shared utility
        return parse_video_ndjson_response(response_text, parent_post_id, statsig_id)

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


class AsyncClientBase(ResponseParser, ABC):
    """
    Abstract base class for asynchronous Grok API clients.

    Implements all async API methods. Subclasses only need to provide:
    - __init__(): Initialize HTTP client (must call super().__init__())
    - __aenter__/__aexit__: Async context manager lifecycle
    - _api_request(): Make authenticated API requests (async)
    - _api_request_text(): Make authenticated request returning raw text (async)
    - _asset_request_head(): Make HEAD request to assets URL (async)

    Uses Bridge Pattern to separate business logic from I/O operations,
    eliminating code duplication between sync and async implementations.
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    def __init__(self):
        """Initialize client with business logic layer."""
        super().__init__()
        self._logic = GrokClientLogic()

    # =========================================================================
    # Abstract methods (must be implemented by subclasses)
    # =========================================================================

    @abstractmethod
    async def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Grok API."""
        pass

    @abstractmethod
    async def _api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Make authenticated request and return raw text response."""
        pass

    @abstractmethod
    async def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL and return Content-Length."""
        pass

    # =========================================================================
    # API 1: list_posts()
    # =========================================================================

    async def list_posts(
        self,
        limit: int = 40,
        source: str = "MEDIA_POST_SOURCE_LIKED",
        include_raw_data: bool = False,
    ) -> list[PostSummary]:
        """
        List posts with basic metadata (async).

        Args:
            limit: Maximum number of posts to return (default: 40)
            source: Filter by source type. Options:
                    - "MEDIA_POST_SOURCE_LIKED": Your liked posts only (default)
                    - None: All public posts
            include_raw_data: If True, include raw API response in each PostSummary.

        Returns:
            List of PostSummary objects
        """
        json_data: dict[str, Any] = {"limit": limit}
        if source:
            json_data["filter"] = {"source": source}
        else:
            json_data["filter"] = {}

        data = await self._api_request("POST", "/rest/media/post/list", json_data)

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

    async def get_post_details(self, post_id: str) -> PostDetails:
        """
        Get full details of a post including all child videos (async).

        Args:
            post_id: Post UUID

        Returns:
            PostDetails object with all metadata and children
        """
        data = await self._api_request("POST", "/rest/media/post/get", {"id": post_id})
        post_data = data.get("post", data)
        return self._parse_post_details(post_data, post_id, raw_data=data)

    # =========================================================================
    # API 3: get_asset_file_size()
    # =========================================================================

    async def get_asset_file_size(self, asset_url: str) -> int:
        """
        Get file size of a Grok asset via HEAD request (async).

        Args:
            asset_url: Full URL to asset on assets.grok.com or imagine-public.x.ai

        Returns:
            File size in bytes
        """
        # Use business logic layer for validation
        self._logic.validate_asset_url(asset_url)
        # Execute I/O operation
        return await self._asset_request_head(asset_url)

    # =========================================================================
    # API 4: validate_auth()
    # =========================================================================

    async def validate_auth(self) -> bool:
        """
        Check if current authentication is working (async).

        Returns:
            True if requests succeed, False otherwise
        """
        try:
            await self._api_request(
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

    async def match_local_video(self, local_path: str | Path) -> VideoMatchResult:
        """
        Match a local grok video to its web counterpart (async).

        Supports two filename formats:
        - Old format: grok-video-{parent_uuid}.mp4 (extracts parent_id)
        - Web format: {video_uuid}.mp4 or {video_uuid}_hd.mp4 (extracts video_id)

        Args:
            local_path: Path to local video file

        Returns:
            VideoMatchResult with parent_id, video_id, mode, new_filename
        """
        local_path = Path(local_path)

        if not local_path.exists():
            raise GrokAPIError(f"File not found: {local_path}")

        filename = local_path.name
        local_size = local_path.stat().st_size

        # Try old format: grok-video-{parent_uuid}.mp4
        old_match = re.match(
            r"grok-video-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            filename,
        )

        # Try web format: {video_uuid}.mp4 or {video_uuid}_hd.mp4 or {video_uuid}_hd (1).mp4
        web_match = re.match(
            r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
            r"(?:_hd)?(?:\s*\(\d+\))?\.mp4$",
            filename,
        )

        if old_match:
            # Old format: grok-video-{uuid}.mp4
            # The UUID could be: (1) parent_id, (2) video_id, or (3) unknown grok internal ID
            extracted_uuid = old_match.group(1)

            # Try 1: Treat as parent_id
            try:
                return await self._match_by_parent_id(extracted_uuid, local_size, filename)
            except GrokNotFoundError:
                pass  # Parent doesn't exist, try next

            # Try 2: Treat as video_id
            try:
                return await self._match_by_video_id(extracted_uuid, local_size, filename)
            except (GrokNotFoundError, GrokAPIError):
                pass  # Video doesn't exist, try fallback

            # Try 3: Fallback - search all liked posts by file size
            return await self._match_by_file_size_via_favorites(
                local_size, filename, hint_uuid=extracted_uuid
            )

        elif web_match:
            # Web format: we have video_id, need to search favorites
            video_id = web_match.group(1)
            return await self._match_by_video_id(video_id, local_size, filename)
        else:
            raise GrokAPIError(
                f"Invalid filename format. Expected 'grok-video-{{uuid}}.mp4' or "
                f"'{{uuid}}.mp4' or '{{uuid}}_hd.mp4', got: {filename}"
            )

    async def _match_by_parent_id(
        self, parent_id: str, local_size: int, filename: str
    ) -> VideoMatchResult:
        """Match video by parent ID (old format)."""
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

        raise GrokAPIError(
            f"No matching video found on web for local file: {filename}\n"
            f"Local size: {local_size} bytes\n"
            f"Parent ID: {parent_id}\n"
            f"Videos checked: {len(videos_to_check)}"
        )

    async def _match_by_video_id(
        self, video_id: str, local_size: int, filename: str
    ) -> VideoMatchResult:
        """Match video by video ID (web format) - O(1) direct lookup with fallback.

        Strategy:
        1. Try direct API call with video_id (O(1) - fast)
        2. If 404, search recent favorites for parent (O(n) - limited scope)

        Uses Bridge Pattern: I/O operations here, business logic in GrokClientLogic.
        """
        # I/O Layer: Fetch post details
        try:
            details = await self.get_post_details(video_id)
        except GrokNotFoundError:
            # Child video post is 404 - try to find parent by searching favorites
            return await self._match_by_video_id_via_favorites(video_id, local_size, filename)
        except GrokAuthError:
            raise
        except Exception as e:
            raise GrokAPIError(
                f"Failed to get video details: {video_id}\n"
                f"Local file: {filename}\n"
                f"Error: {e}"
            ) from e

        # Business Logic Layer: Extract media URL
        url = self._logic.extract_media_url_from_details(details, video_id, filename)

        # I/O Layer: Fetch file size
        web_size = await self.get_asset_file_size(url)

        # Business Logic Layer: Extract parent info, verify size, build result
        parent_id, is_parent_video = self._logic.extract_parent_info_from_details(details, video_id)
        self._logic.verify_file_size_match(video_id, filename, local_size, web_size)
        return self._logic.build_video_match_result(
            parent_id, video_id, is_parent_video, details, local_size
        )

    async def _match_by_video_id_via_favorites(
        self, video_id: str, local_size: int, filename: str, max_posts: int = 200
    ) -> VideoMatchResult:
        """Fallback: Search recent favorites to find parent of orphaned child video.

        This handles the case where child video's post page is 404, but the video
        still exists in parent's children list.

        Args:
            video_id: Child video ID to search for
            local_size: Local file size
            filename: Local filename
            max_posts: Maximum number of recent posts to search (default: 100)

        Returns:
            VideoMatchResult if found

        Raises:
            GrokAPIError: If video not found in recent favorites
        """
        # Search recent favorites for this video
        posts = await self.list_posts(limit=max_posts, source="MEDIA_POST_SOURCE_LIKED")

        for post_summary in posts:
            try:
                # Get full details to check children
                details = await self.get_post_details(post_summary.id)

                # Check if this video is in children
                for child in details.children:
                    if child.id == video_id:
                        # Found it! Build result
                        parent_id = post_summary.id
                        url = child.hd_media_url or child.media_url

                        if not url:
                            continue

                        # Get file size
                        try:
                            web_size = await self.get_asset_file_size(url)
                        except Exception:
                            # If we can't get size, skip size verification
                            web_size = local_size

                        # Verify size match
                        self._logic.verify_file_size_match(video_id, filename, local_size, web_size)

                        # Build result
                        return VideoMatchResult(
                            parent_id=parent_id,
                            video_id=video_id,
                            is_parent_video=False,
                            mode=details.mode,
                            original_prompt=child.original_prompt,
                            file_size=local_size,
                            new_filename=f"grok-video_{parent_id}_{video_id}.mp4",
                        )
            except Exception:
                # Skip posts that fail to load
                continue

        # Not found in recent favorites
        raise GrokAPIError(
            f"Video not found in recent {max_posts} favorites.\n"
            f"Video ID: {video_id}\n"
            f"Local file: {filename}\n\n"
            f"This video's post page is 404, and it wasn't found in your recent favorites.\n"
            f"Possible solutions:\n"
            f"1. The video may be in older favorites - increase search limit\n"
            f"2. Manually provide parent_id if you know it\n"
            f"3. Visit https://grok.com/imagine/post/{video_id} to check if it exists"
        )

    async def _match_by_file_size_via_favorites(
        self, local_size: int, filename: str, hint_uuid: str | None = None, max_posts: int = 200
    ) -> VideoMatchResult:
        """Fallback: Search all liked posts to find video by file size.

        This handles the case where the UUID in the filename doesn't match any
        known parent_id or video_id (e.g., grok.com uses internal session IDs
        for download filenames).

        Args:
            local_size: Local file size in bytes
            filename: Local filename (for error messages)
            hint_uuid: The UUID extracted from filename (for logging)
            max_posts: Maximum number of posts to search (default: 100)

        Returns:
            VideoMatchResult if found

        Raises:
            GrokAPIError: If no matching video found by file size
        """
        posts = await self.list_posts(limit=max_posts, source="MEDIA_POST_SOURCE_LIKED")

        for post_summary in posts:
            try:
                details = await self.get_post_details(post_summary.id)

                # Check parent video (for txt2vid posts)
                if details.mode == GenerationMode.TEXT_TO_VIDEO and details.hd_media_url:
                    try:
                        web_size = await self.get_asset_file_size(details.hd_media_url)
                        if web_size == local_size:
                            return VideoMatchResult(
                                parent_id=details.id,
                                video_id=details.id,
                                is_parent_video=True,
                                mode=details.mode,
                                original_prompt=details.original_prompt,
                                file_size=local_size,
                                new_filename=f"grok-video_{details.id}_{details.id}.mp4",
                            )
                    except Exception:
                        pass

                # Check all children
                for child in details.children:
                    url = child.hd_media_url or child.media_url
                    if not url:
                        continue

                    try:
                        web_size = await self.get_asset_file_size(url)
                        if web_size == local_size:
                            return VideoMatchResult(
                                parent_id=post_summary.id,
                                video_id=child.id,
                                is_parent_video=False,
                                mode=details.mode,
                                original_prompt=child.original_prompt,
                                file_size=local_size,
                                new_filename=f"grok-video_{post_summary.id}_{child.id}.mp4",
                            )
                    except Exception:
                        continue

            except Exception:
                continue

        # Not found
        hint_msg = f" (extracted UUID: {hint_uuid})" if hint_uuid else ""
        raise GrokAPIError(
            f"No matching video found by file size in recent {max_posts} favorites.\n"
            f"Local file: {filename}{hint_msg}\n"
            f"Local size: {local_size} bytes\n\n"
            f"The UUID in the filename doesn't match any known post or video ID.\n"
            f"Possible causes:\n"
            f"1. The video's parent post is not in your favorites - add it first\n"
            f"2. The video may have been deleted from grok.com\n"
            f"3. Try increasing max_posts if you have many favorites"
        )

    # =========================================================================
    # API 6: favorite_post()
    # =========================================================================

    async def favorite_post(self, post_id: str) -> bool:
        """
        Add a post to favorites (save it).

        Args:
            post_id: Post UUID to favorite

        Returns:
            True if successful
        """
        await self._api_request("POST", MEDIA_POST_LIKE_ENDPOINT, {"id": post_id})
        return True

    # =========================================================================
    # API 7: unfavorite_post()
    # =========================================================================

    async def unfavorite_post(self, post_id: str) -> bool:
        """
        Remove a post from favorites.

        Args:
            post_id: Post UUID to unfavorite

        Returns:
            True if successful
        """
        await self._api_request("POST", MEDIA_POST_UNLIKE_ENDPOINT, {"id": post_id})
        return True

    # =========================================================================
    # API 8: create_video_from_image()
    # =========================================================================

    async def create_video_from_image(
        self,
        image_url: str,
        parent_post_id: str,
        aspect_ratio: str = "2:3",
        video_length: int = 6,
        statsig_id: str | None = None,
        preset: VideoPreset | str = "normal",
    ) -> VideoGenerationResult:
        """
        Generate a video from an image using Grok's chat API (async).

        Args:
            image_url: Full URL to image on imagine-public.x.ai
            parent_post_id: Parent image post UUID
            aspect_ratio: Video aspect ratio (default: "2:3")
            video_length: Duration in seconds (default: 6)
            preset: Video generation preset (default: "normal")
            statsig_id: Style seed for video generation

        Returns:
            VideoGenerationResult with video_id, moderated status, etc.
        """
        # Generate or use provided statsig_id
        if statsig_id is None:
            statsig_id = generate_statsig_id()

        # Build payload using shared utility
        mode_value = resolve_preset(preset)
        payload = build_video_payload(
            image_url, parent_post_id, mode_value, aspect_ratio, video_length
        )

        # Get raw text response (NDJSON streaming format)
        response_text = await self._api_request_text(
            "POST",
            "/rest/app-chat/conversations/new",
            payload,
            extra_headers={"x-statsig-id": statsig_id},
        )

        # Parse using shared utility
        return parse_video_ndjson_response(response_text, parent_post_id, statsig_id)
