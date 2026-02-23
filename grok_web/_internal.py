"""
Internal implementation for Grok Web Connector.

Contains:
- ResponseParser: Pure data transformation (API JSON → Python objects)
- Utility functions: Response parsers
- Endpoint constants

Do not import from this module directly. Use the public API from grok_web instead.
"""

import re
from datetime import datetime
from typing import Any

from .exceptions import GrokAPIError, GrokRateLimitError
from .models import (
    ChildVideo,
    GenerationMode,
    PostDetails,
    PostSummary,
    VideoGenerationResult,
    VideoMatchResult,
)

# =============================================================================
# API Endpoint Constants
# =============================================================================

MEDIA_POST_LIKE_ENDPOINT = "/rest/media/post/like"
MEDIA_POST_UNLIKE_ENDPOINT = "/rest/media/post/unlike"
MEDIA_POST_LIST_ENDPOINT = "/rest/media/post/list"
MEDIA_POST_GET_ENDPOINT = "/rest/media/post/get"

# =============================================================================
# Shared Utilities
# =============================================================================


def parse_video_ndjson_response(
    response_text: str,
    parent_post_id: str,
    statsig_id: str,
) -> VideoGenerationResult:
    """Parse NDJSON response from video generation API."""
    import json

    conversation_id = None
    video_result = None

    for line in response_text.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)

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
# Response Parser - Pure data transformation + helper methods
# =============================================================================


class ResponseParser:
    """Parses Grok API responses into Python objects.

    Also provides pure helper methods for video matching logic
    (no I/O, just data manipulation).
    """

    ASSETS_URL = "https://assets.grok.com"

    # =========================================================================
    # Parsing methods
    # =========================================================================

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

    # =========================================================================
    # Pure helper methods for video matching (no I/O)
    # =========================================================================

    def _validate_asset_url(self, asset_url: str) -> None:
        """Validate asset URL format."""
        if not asset_url:
            raise GrokAPIError("Asset URL is empty")

        if not (
            asset_url.startswith(self.ASSETS_URL)
            or asset_url.startswith("https://imagine-public.x.ai/")
        ):
            raise GrokAPIError(
                f"Invalid asset URL. Expected {self.ASSETS_URL}/... or "
                f"https://imagine-public.x.ai/..., got: {asset_url[:50]}..."
            )

    def _extract_parent_info(self, details: PostDetails, video_id: str) -> tuple[str, bool]:
        """Extract parent ID and parent status from PostDetails."""
        raw_post = details.raw_data.get("post", details.raw_data) if details.raw_data else {}
        original_post_id = raw_post.get("originalPostId")

        if original_post_id and original_post_id != video_id:
            return original_post_id, False
        else:
            return video_id, True

    def _verify_file_size_match(
        self, video_id: str, filename: str, local_size: int, web_size: int
    ) -> None:
        """Verify local and web file sizes match."""
        if web_size != local_size:
            raise GrokAPIError(
                f"File size mismatch for video: {video_id}\n"
                f"Local file: {filename}\n"
                f"Local size: {local_size}, Web size: {web_size}"
            )

    def _build_video_match_result(
        self,
        parent_id: str,
        video_id: str,
        is_parent_video: bool,
        details: PostDetails,
        local_size: int,
    ) -> VideoMatchResult:
        """Build VideoMatchResult from components."""
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

    def _extract_media_url(self, details: PostDetails, video_id: str, filename: str) -> str:
        """Extract media URL from PostDetails."""
        url = details.hd_media_url or details.media_url
        if not url:
            raise GrokAPIError(f"No media URL found for video: {video_id}\nLocal file: {filename}")
        return url

    @staticmethod
    def _parse_video_filename(filename: str) -> tuple[str | None, str | None]:
        """Parse video filename to extract UUIDs.

        Returns:
            (format_type, uuid) where format_type is 'old' or 'web', or (None, None).
        """
        # Try old format: grok-video-{parent_uuid}.mp4
        old_match = re.match(
            r"grok-video-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            filename,
        )
        if old_match:
            return "old", old_match.group(1)

        # Try web format: {video_uuid}.mp4 or {video_uuid}_hd.mp4
        web_match = re.match(
            r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
            r"(?:_hd)?(?:\s*\(\d+\))?\.mp4$",
            filename,
        )
        if web_match:
            return "web", web_match.group(1)

        return None, None
