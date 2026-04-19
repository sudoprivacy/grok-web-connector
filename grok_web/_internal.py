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
    MODE_IMG2VID,
    MODE_TXT2VID,
    MODE_UNKNOWN,
    MODE_UPLOAD2VID,
    ChildPost,
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

    def _detect_generation_mode(self, post_data: dict) -> str:
        """Detect generation mode from post metadata.

        Returns plain string: 'txt2img', 'img2vid', 'txt2vid', 'upload2vid', or 'unknown'.
        """
        media_type = post_data.get("mediaType", "")
        prompt = post_data.get("prompt")
        mode = post_data.get("mode")

        if media_type == "MEDIA_POST_TYPE_VIDEO":
            if mode == "text":
                return MODE_TXT2VID
            return MODE_UNKNOWN

        if media_type == "MEDIA_POST_TYPE_IMAGE":
            if prompt:
                return MODE_IMG2VID
            else:
                return MODE_UPLOAD2VID

        return MODE_UNKNOWN

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
        """Parse API response into PostDetails with all children.

        Grok's /rest/media/post/get response represents the full edit
        tree via three arrays:

        - ``images[]``  — every image in the edit tree, INCLUDING this
                          post itself if it is an image.
        - ``videos[]``  — every video in the edit tree, INCLUDING this
                          post itself if it is a video.
        - ``childPosts[]`` — legacy field, strict children (does not
                             include self). Can be incomplete under the
                             post 2026 UI (observed missing both
                             edit-image and video entries that DO appear
                             in ``images[]`` / ``videos[]``).

        Parent/child lineage is expressed by each entry's
        ``originalPostId`` — the image or video it was generated from.
        A video whose ``originalPostId`` points at an entry in
        ``images[]`` was made from that image.

        Strategy: union ``images[]`` + ``videos[]`` (the authoritative
        arrays), de-duplicate by id, drop self-references. Fall back to
        ``childPosts[]`` only for entries that the new arrays are
        missing — keeps us robust if Grok rolls back.
        """
        mode = self._detect_generation_mode(data)

        def _to_child(entry: dict) -> ChildPost | None:
            media_type = entry.get("mediaType")
            if media_type not in ("MEDIA_POST_TYPE_VIDEO", "MEDIA_POST_TYPE_IMAGE"):
                return None
            return ChildPost(
                id=entry.get("id", ""),
                media_type=media_type,
                original_post_id=entry.get("originalPostId") or post_id,
                original_prompt=entry.get("originalPrompt"),
                prompt=entry.get("prompt"),
                media_url=entry.get("mediaUrl"),
                hd_media_url=entry.get("hdMediaUrl"),
                thumbnail_url=entry.get("thumbnailImageUrl"),
                created_at=self._parse_timestamp(entry.get("createTime")),
                resolution=entry.get("resolution"),
                duration=entry.get("duration"),
                model_name=entry.get("modelName"),
                mode=entry.get("mode"),
            )

        children: list[ChildPost] = []
        seen_ids: set[str] = {post_id}  # exclude self to keep ChildPost semantics
        # Prefer the new top-level arrays first.
        for bucket_key in ("images", "videos"):
            for entry in data.get(bucket_key) or []:
                eid = entry.get("id")
                if not eid or eid in seen_ids:
                    continue
                child = _to_child(entry)
                if child is None:
                    continue
                seen_ids.add(eid)
                children.append(child)
        # Fall back to childPosts[] for anything still missing (legacy +
        # defensive). Skip entries we already captured.
        for entry in data.get("childPosts") or []:
            eid = entry.get("id")
            if not eid or eid in seen_ids:
                continue
            child = _to_child(entry)
            if child is None:
                continue
            seen_ids.add(eid)
            children.append(child)

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
            original_post_id=data.get("originalPostId"),
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
