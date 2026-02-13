"""
Internal implementation for Grok Web Connector.

This module uses the Effect Pattern to eliminate code duplication between
sync and async clients. Business logic is written once as generators that
yield I/O effects, which are then executed by sync or async executors.

Do not import from this module directly. Use the public API from grok_web instead.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

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

T = TypeVar("T")

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
    video_length: int = 10,
    adjustment_prompt: str | None = None,
    video_resolution: str = "720",
) -> dict:
    """Build the payload for video generation API."""
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
                        "videoResolution": video_resolution,
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
# Effect Types - Describe I/O operations without executing them
# =============================================================================


@dataclass
class Effect:
    """Base class for I/O effects."""

    pass


@dataclass
class ApiRequest(Effect):
    """Request to make an API call returning JSON."""

    method: str
    endpoint: str
    json_data: dict | None = None


@dataclass
class ApiRequestText(Effect):
    """Request to make an API call returning raw text."""

    method: str
    endpoint: str
    json_data: dict | None = None
    extra_headers: dict | None = None


@dataclass
class AssetHeadRequest(Effect):
    """Request to get Content-Length of an asset URL."""

    asset_url: str


# Type alias for generator-based business logic
EffectGenerator = Generator[Effect, Any, T]


# =============================================================================
# Response Parser - Pure data transformation
# =============================================================================


class ResponseParser:
    """Parses Grok API responses into Python objects."""

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
# Business Logic - Generator-based, written once, used by both sync and async
# =============================================================================


class ClientLogic(ResponseParser):
    """
    Business logic using generators for I/O operations.

    Methods yield Effect objects to request I/O, and receive results back.
    This allows the same logic to be executed by both sync and async clients.
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    # =========================================================================
    # Pure helper methods (no I/O)
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

    # =========================================================================
    # Generator-based API methods (yield I/O effects)
    # =========================================================================

    def list_posts(
        self,
        limit: int = 40,
        source: str = "MEDIA_POST_SOURCE_LIKED",
        include_raw_data: bool = False,
    ) -> EffectGenerator[list[PostSummary]]:
        """List posts with basic metadata."""
        json_data: dict[str, Any] = {"limit": limit}
        if source:
            json_data["filter"] = {"source": source}
        else:
            json_data["filter"] = {}

        data = yield ApiRequest("POST", MEDIA_POST_LIST_ENDPOINT, json_data)

        posts = []
        for item in data.get("posts", []):
            try:
                summary = self._parse_post_summary(item, include_raw_data=include_raw_data)
                posts.append(summary)
            except Exception:
                continue

        return posts

    def get_post_details(self, post_id: str) -> EffectGenerator[PostDetails]:
        """Get full details of a post including all child videos."""
        data = yield ApiRequest("POST", MEDIA_POST_GET_ENDPOINT, {"id": post_id})
        post_data = data.get("post", data)
        return self._parse_post_details(post_data, post_id, raw_data=data)

    def get_asset_file_size(self, asset_url: str) -> EffectGenerator[int]:
        """Get file size of a Grok asset via HEAD request."""
        self._validate_asset_url(asset_url)
        size = yield AssetHeadRequest(asset_url)
        return size

    def validate_auth(self) -> EffectGenerator[bool]:
        """Check if current authentication is working."""
        try:
            yield ApiRequest("POST", MEDIA_POST_LIST_ENDPOINT, {"limit": 1, "filter": {}})
            return True
        except (GrokAuthError, GrokAPIError):
            return False

    def favorite_post(self, post_id: str) -> EffectGenerator[bool]:
        """Add a post to favorites."""
        yield ApiRequest("POST", MEDIA_POST_LIKE_ENDPOINT, {"id": post_id})
        return True

    def unfavorite_post(self, post_id: str) -> EffectGenerator[bool]:
        """Remove a post from favorites."""
        yield ApiRequest("POST", MEDIA_POST_UNLIKE_ENDPOINT, {"id": post_id})
        return True

    def create_video_from_image(
        self,
        image_url: str,
        parent_post_id: str,
        aspect_ratio: str = "2:3",
        video_length: int = 10,
        statsig_id: str | None = None,
        preset: VideoPreset | str = "normal",
        video_resolution: str = "720",
    ) -> EffectGenerator[VideoGenerationResult]:
        """Generate a video from an image using Grok's chat API."""
        if statsig_id is None:
            statsig_id = generate_statsig_id()

        mode_value = resolve_preset(preset)
        payload = build_video_payload(
            image_url,
            parent_post_id,
            mode_value,
            aspect_ratio,
            video_length,
            video_resolution=video_resolution,
        )

        response_text = yield ApiRequestText(
            "POST",
            "/rest/app-chat/conversations/new",
            payload,
            extra_headers={"x-statsig-id": statsig_id},
        )

        return parse_video_ndjson_response(response_text, parent_post_id, statsig_id)

    def match_local_video(self, local_path: str | Path) -> EffectGenerator[VideoMatchResult]:
        """Match a local grok video to its web counterpart."""
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

        # Try web format: {video_uuid}.mp4 or {video_uuid}_hd.mp4
        web_match = re.match(
            r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
            r"(?:_hd)?(?:\s*\(\d+\))?\.mp4$",
            filename,
        )

        if old_match:
            extracted_uuid = old_match.group(1)

            # Try 1: Treat as parent_id
            try:
                result = yield from self._match_by_parent_id(extracted_uuid, local_size, filename)
                return result
            except (GrokNotFoundError, GrokAPIError):
                pass

            # Try 2: Treat as video_id
            try:
                result = yield from self._match_by_video_id(extracted_uuid, local_size, filename)
                return result
            except (GrokNotFoundError, GrokAPIError):
                pass

            # Try 3: Fallback - search by file size
            result = yield from self._match_by_file_size_via_favorites(
                local_size, filename, hint_uuid=extracted_uuid
            )
            return result

        elif web_match:
            video_id = web_match.group(1)
            result = yield from self._match_by_video_id(video_id, local_size, filename)
            return result
        else:
            raise GrokAPIError(
                f"Invalid filename format. Expected 'grok-video-{{uuid}}.mp4' or "
                f"'{{uuid}}.mp4' or '{{uuid}}_hd.mp4', got: {filename}"
            )

    def _match_by_parent_id(
        self, parent_id: str, local_size: int, filename: str
    ) -> EffectGenerator[VideoMatchResult]:
        """Match video by parent ID."""
        details = yield from self.get_post_details(parent_id)

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
                web_size = yield from self.get_asset_file_size(video["url"])
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

    def _match_by_video_id(
        self, video_id: str, local_size: int, filename: str
    ) -> EffectGenerator[VideoMatchResult]:
        """Match video by video ID - O(1) direct lookup."""
        try:
            details = yield from self.get_post_details(video_id)
        except GrokNotFoundError:
            # Child video post is 404 - try to find parent in favorites
            result = yield from self._match_by_video_id_via_favorites(
                video_id, local_size, filename
            )
            return result
        except GrokAuthError:
            raise
        except Exception as e:
            raise GrokAPIError(
                f"Failed to get video details: {video_id}\n"
                f"Local file: {filename}\n"
                f"Error: {e}"
            ) from e

        url = self._extract_media_url(details, video_id, filename)
        web_size = yield from self.get_asset_file_size(url)

        parent_id, is_parent_video = self._extract_parent_info(details, video_id)
        self._verify_file_size_match(video_id, filename, local_size, web_size)
        return self._build_video_match_result(
            parent_id, video_id, is_parent_video, details, local_size
        )

    def _match_by_video_id_via_favorites(
        self, video_id: str, local_size: int, filename: str, max_posts: int = 200
    ) -> EffectGenerator[VideoMatchResult]:
        """Search recent favorites to find parent of orphaned child video."""
        posts = yield from self.list_posts(limit=max_posts, source="MEDIA_POST_SOURCE_LIKED")

        for post_summary in posts:
            try:
                details = yield from self.get_post_details(post_summary.id)

                for child in details.children:
                    if child.id == video_id:
                        parent_id = post_summary.id
                        url = child.hd_media_url or child.media_url

                        if not url:
                            continue

                        try:
                            web_size = yield from self.get_asset_file_size(url)
                        except Exception:
                            web_size = local_size

                        self._verify_file_size_match(video_id, filename, local_size, web_size)

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
                continue

        raise GrokAPIError(
            f"Video not found in recent {max_posts} favorites.\n"
            f"Video ID: {video_id}\n"
            f"Local file: {filename}\n"
        )

    def _match_by_file_size_via_favorites(
        self, local_size: int, filename: str, hint_uuid: str | None = None, max_posts: int = 200
    ) -> EffectGenerator[VideoMatchResult]:
        """Search all liked posts to find video by file size."""
        posts = yield from self.list_posts(limit=max_posts, source="MEDIA_POST_SOURCE_LIKED")

        for post_summary in posts:
            try:
                details = yield from self.get_post_details(post_summary.id)

                # Check parent video (for txt2vid posts)
                if details.mode == GenerationMode.TEXT_TO_VIDEO and details.hd_media_url:
                    try:
                        web_size = yield from self.get_asset_file_size(details.hd_media_url)
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
                        web_size = yield from self.get_asset_file_size(url)
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

        hint_msg = f" (extracted UUID: {hint_uuid})" if hint_uuid else ""
        raise GrokAPIError(
            f"No matching video found by file size in recent {max_posts} favorites.\n"
            f"Local file: {filename}{hint_msg}\n"
            f"Local size: {local_size} bytes\n"
        )


# =============================================================================
# Executor Base Classes - Execute generators with sync or async I/O
# =============================================================================


class SyncClientBase(ABC):
    """
    Abstract base class for synchronous Grok API clients.

    Subclasses only need to provide:
    - __init__(): Initialize HTTP client
    - _api_request(): Make authenticated API requests
    - _api_request_text(): Make authenticated request returning raw text
    - _asset_request_head(): Make HEAD request to assets URL
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    def __init__(self):
        """Initialize client with business logic layer."""
        self._logic = ClientLogic()

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
    def _api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Make authenticated request and return raw text response."""
        pass

    @abstractmethod
    def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL and return Content-Length."""
        pass

    # =========================================================================
    # Effect Executor
    # =========================================================================

    def _execute(self, gen: EffectGenerator[T]) -> T:
        """Execute a generator by handling effects synchronously."""
        result = None
        try:
            effect = gen.send(None)
            while True:
                try:
                    result = self._handle_effect(effect)
                    effect = gen.send(result)
                except StopIteration:
                    raise
                except Exception as e:
                    # Send exception into generator so it can handle it
                    effect = gen.throw(e)
        except StopIteration as e:
            return e.value

    def _handle_effect(self, effect: Effect) -> Any:
        """Handle a single effect synchronously."""
        if isinstance(effect, ApiRequest):
            return self._api_request(effect.method, effect.endpoint, effect.json_data)
        elif isinstance(effect, ApiRequestText):
            return self._api_request_text(
                effect.method, effect.endpoint, effect.json_data, effect.extra_headers
            )
        elif isinstance(effect, AssetHeadRequest):
            return self._asset_request_head(effect.asset_url)
        else:
            raise ValueError(f"Unknown effect type: {type(effect)}")

    # =========================================================================
    # Public API - Simple delegation to business logic
    # =========================================================================

    def list_posts(
        self,
        limit: int = 40,
        source: str = "MEDIA_POST_SOURCE_LIKED",
        include_raw_data: bool = False,
    ) -> list[PostSummary]:
        """List posts with basic metadata."""
        return self._execute(self._logic.list_posts(limit, source, include_raw_data))

    def get_post_details(self, post_id: str) -> PostDetails:
        """Get full details of a post including all child videos."""
        return self._execute(self._logic.get_post_details(post_id))

    def get_asset_file_size(self, asset_url: str) -> int:
        """Get file size of a Grok asset via HEAD request."""
        return self._execute(self._logic.get_asset_file_size(asset_url))

    def validate_auth(self) -> bool:
        """Check if current authentication is working."""
        return self._execute(self._logic.validate_auth())

    def match_local_video(self, local_path: str | Path) -> VideoMatchResult:
        """Match a local grok video to its web counterpart."""
        return self._execute(self._logic.match_local_video(local_path))

    def favorite_post(self, post_id: str) -> bool:
        """Add a post to favorites."""
        return self._execute(self._logic.favorite_post(post_id))

    def unfavorite_post(self, post_id: str) -> bool:
        """Remove a post from favorites."""
        return self._execute(self._logic.unfavorite_post(post_id))

    def create_video_from_image(
        self,
        image_url: str,
        parent_post_id: str,
        aspect_ratio: str = "2:3",
        video_length: int = 10,
        statsig_id: str | None = None,
        preset: VideoPreset | str = "normal",
        video_resolution: str = "720",
    ) -> VideoGenerationResult:
        """Generate a video from an image using Grok's chat API."""
        return self._execute(
            self._logic.create_video_from_image(
                image_url,
                parent_post_id,
                aspect_ratio,
                video_length,
                statsig_id,
                preset,
                video_resolution,
            )
        )


class AsyncClientBase(ABC):
    """
    Abstract base class for asynchronous Grok API clients.

    Subclasses only need to provide:
    - __init__(): Initialize HTTP client
    - __aenter__/__aexit__: Async context manager lifecycle
    - _api_request(): Make authenticated API requests (async)
    - _api_request_text(): Make authenticated request returning raw text (async)
    - _asset_request_head(): Make HEAD request to assets URL (async)
    """

    BASE_URL = "https://grok.com"
    ASSETS_URL = "https://assets.grok.com"

    def __init__(self):
        """Initialize client with business logic layer."""
        self._logic = ClientLogic()

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
    # Effect Executor
    # =========================================================================

    async def _execute(self, gen: EffectGenerator[T]) -> T:
        """Execute a generator by handling effects asynchronously."""
        result = None
        try:
            effect = gen.send(None)
            while True:
                try:
                    result = await self._handle_effect(effect)
                    effect = gen.send(result)
                except StopIteration:
                    raise
                except Exception as e:
                    # Send exception into generator so it can handle it
                    effect = gen.throw(e)
        except StopIteration as e:
            return e.value

    async def _handle_effect(self, effect: Effect) -> Any:
        """Handle a single effect asynchronously."""
        if isinstance(effect, ApiRequest):
            return await self._api_request(effect.method, effect.endpoint, effect.json_data)
        elif isinstance(effect, ApiRequestText):
            return await self._api_request_text(
                effect.method, effect.endpoint, effect.json_data, effect.extra_headers
            )
        elif isinstance(effect, AssetHeadRequest):
            return await self._asset_request_head(effect.asset_url)
        else:
            raise ValueError(f"Unknown effect type: {type(effect)}")

    # =========================================================================
    # Public API - Simple delegation to business logic
    # =========================================================================

    async def list_posts(
        self,
        limit: int = 40,
        source: str = "MEDIA_POST_SOURCE_LIKED",
        include_raw_data: bool = False,
    ) -> list[PostSummary]:
        """List posts with basic metadata."""
        return await self._execute(self._logic.list_posts(limit, source, include_raw_data))

    async def get_post_details(self, post_id: str) -> PostDetails:
        """Get full details of a post including all child videos."""
        return await self._execute(self._logic.get_post_details(post_id))

    async def get_asset_file_size(self, asset_url: str) -> int:
        """Get file size of a Grok asset via HEAD request."""
        return await self._execute(self._logic.get_asset_file_size(asset_url))

    async def validate_auth(self) -> bool:
        """Check if current authentication is working."""
        return await self._execute(self._logic.validate_auth())

    async def match_local_video(self, local_path: str | Path) -> VideoMatchResult:
        """Match a local grok video to its web counterpart."""
        return await self._execute(self._logic.match_local_video(local_path))

    async def favorite_post(self, post_id: str) -> bool:
        """Add a post to favorites."""
        return await self._execute(self._logic.favorite_post(post_id))

    async def unfavorite_post(self, post_id: str) -> bool:
        """Remove a post from favorites."""
        return await self._execute(self._logic.unfavorite_post(post_id))

    async def create_video_from_image(
        self,
        image_url: str,
        parent_post_id: str,
        aspect_ratio: str = "2:3",
        video_length: int = 10,
        statsig_id: str | None = None,
        preset: VideoPreset | str = "normal",
        video_resolution: str = "720",
    ) -> VideoGenerationResult:
        """Generate a video from an image using Grok's chat API."""
        return await self._execute(
            self._logic.create_video_from_image(
                image_url,
                parent_post_id,
                aspect_ratio,
                video_length,
                statsig_id,
                preset,
                video_resolution,
            )
        )


# =============================================================================
# Backward Compatibility - Keep old names as aliases
# =============================================================================

# These are kept for backward compatibility with existing code
GrokClientLogic = ClientLogic
ResponseParser = ResponseParser
