"""Data models for Grok Web Connector."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field


class GenerationMode(str, Enum):
    """Grok Imagine generation modes (4 pipelines)."""

    TEXT_TO_IMAGE = "txt2img"  # Text→Image (starting point for long videos)
    GROK_IMAGE_TO_VIDEO = "img2vid"  # Grok-generated image→Video
    TEXT_TO_VIDEO = "txt2vid"  # Text→Video directly
    UPLOAD_IMAGE_TO_VIDEO = "upload2vid"  # Upload external image→Video
    UNKNOWN = "unknown"


class VideoPreset(str, Enum):
    """Video generation presets (UI buttons: Normal, Fun, Spicy)."""

    NORMAL = "normal"  # Standard video generation
    FUN = "extremely-crazy"  # More dynamic/creative motion
    SPICY = "extremely-spicy-or-crazy"  # Most permissive content filter


class ChildPost(BaseModel):
    """A child post (image or video) in a post's childPosts array.

    In Grok, everything is a post. A root image post can have child posts
    that are either edited image variants or generated videos.
    ``original_post_id`` points to the immediate parent (the post this was
    generated from), which may be the root or an edited image.
    """

    id: str = Field(..., description="Child post UUID")
    media_type: str = Field(..., description="MEDIA_POST_TYPE_IMAGE or MEDIA_POST_TYPE_VIDEO")
    original_post_id: str = Field(
        ..., description="Post this was generated from (immediate parent)"
    )

    # Prompts
    original_prompt: str | None = Field(None, description="Generation/edit prompt")
    prompt: str | None = Field(None, description="Image generation prompt")

    # URLs
    media_url: str | None = Field(None, description="Media URL (image or video)")
    hd_media_url: str | None = Field(None, description="HD media URL")
    thumbnail_url: str | None = Field(None, description="Thumbnail image URL")

    # Metadata
    created_at: datetime | None = Field(None, description="Creation timestamp (UTC)")
    resolution: dict[str, int] | None = Field(None, description="Media resolution {width, height}")
    duration: int | None = Field(None, description="Duration in ms (videos only)")
    model_name: str | None = Field(None, description="Model used (e.g., imagine_h_1)")
    mode: str | None = Field(None, description="Generation mode: 'custom', 'normal', etc.")

    @computed_field
    @property
    def is_image(self) -> bool:
        """True if this is an image post."""
        return self.media_type == "MEDIA_POST_TYPE_IMAGE"

    @computed_field
    @property
    def is_video(self) -> bool:
        """True if this is a video post."""
        return self.media_type == "MEDIA_POST_TYPE_VIDEO"

    @computed_field
    @property
    def web_url(self) -> str:
        """Direct web URL to this post."""
        return f"https://grok.com/imagine/post/{self.id}"

    @computed_field
    @property
    def parent_web_url(self) -> str:
        """Web URL to the parent post."""
        return f"https://grok.com/imagine/post/{self.original_post_id}"

    @computed_field
    @property
    def best_media_url(self) -> str | None:
        """Best available media URL (HD preferred)."""
        return self.hd_media_url or self.media_url


# Backward compat
ChildVideo = ChildPost


class PostSummary(BaseModel):
    """Summary of a post for list_posts() response."""

    id: str = Field(..., description="Post UUID")
    mode: GenerationMode = Field(..., description="Generation mode")

    # Preview info
    prompt_preview: str | None = Field(None, description="First 100 chars of prompt")
    video_count: int = Field(0, description="Number of child videos")

    # Timestamps
    created_at: datetime | None = Field(None, description="Creation timestamp (UTC)")

    # Media type
    media_type: str | None = Field(
        None, description="MEDIA_POST_TYPE_IMAGE or MEDIA_POST_TYPE_VIDEO"
    )

    # Raw data for debugging
    raw_data: dict[str, Any] | None = Field(None, description="Raw API response for this post")

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for this post."""
        return f"https://grok.com/imagine/post/{self.id}"


class PostDetails(BaseModel):
    """Full details of a post for get_post_details() response."""

    id: str = Field(..., description="Post UUID")
    user_id: str | None = Field(None, description="Owner's user UUID")
    mode: GenerationMode = Field(..., description="Detected generation mode")

    # Parent post info
    media_type: str | None = Field(
        None, description="MEDIA_POST_TYPE_IMAGE or MEDIA_POST_TYPE_VIDEO"
    )
    prompt: str | None = Field(None, description="Image generation prompt (for img2vid mode)")
    original_prompt: str | None = Field(None, description="Video prompt (for txt2vid mode)")

    # URLs
    media_url: str | None = Field(None, description="Parent media URL (image or video)")
    hd_media_url: str | None = Field(None, description="HD media URL")
    thumbnail_url: str | None = Field(None, description="Thumbnail URL")

    # Metadata
    created_at: datetime | None = Field(None, description="Creation timestamp (UTC)")
    resolution: dict[str, int] | None = Field(None, description="Media resolution")
    model_name: str | None = Field(None, description="Model used (e.g., imagine_x_1)")

    # Child posts (images and videos, flat list from API)
    children: list[ChildPost] = Field(
        default_factory=list, description="All child posts (images and videos)"
    )

    # Original post ID (None for root posts)
    original_post_id: str | None = Field(None, description="Parent post ID (None if root)")

    # Raw data for debugging
    raw_data: dict[str, Any] | None = Field(None, description="Raw API response")

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for this post."""
        return f"https://grok.com/imagine/post/{self.id}"

    @computed_field
    @property
    def is_root(self) -> bool:
        """True if this is a root post (no parent)."""
        return self.original_post_id is None

    @computed_field
    @property
    def video_count(self) -> int:
        """Number of child video posts."""
        return sum(1 for c in self.children if c.is_video)

    @computed_field
    @property
    def image_count(self) -> int:
        """Number of child image posts (edited variants)."""
        return sum(1 for c in self.children if c.is_image)

    @property
    def has_children(self) -> bool:
        """Check if this post has any child posts."""
        return len(self.children) > 0

    @property
    def image_children(self) -> list["ChildPost"]:
        """Child image posts (edited variants)."""
        return [c for c in self.children if c.is_image]

    @property
    def video_children(self) -> list["ChildPost"]:
        """Child video posts."""
        return [c for c in self.children if c.is_video]

    def videos_by_source(self) -> dict[str, list["ChildPost"]]:
        """Group child videos by their source image post ID (original_post_id).

        Returns:
            Dict mapping source post_id → list of video ChildPosts.
        """
        groups: dict[str, list[ChildPost]] = {}
        for child in self.children:
            if child.is_video:
                groups.setdefault(child.original_post_id, []).append(child)
        return groups

    def find_video_source(self, video_id: str) -> str | None:
        """Find which image post a video was generated from.

        Args:
            video_id: The child video post UUID

        Returns:
            The source image post_id (original_post_id), or None if not found
        """
        for child in self.children:
            if child.id == video_id:
                return child.original_post_id
        return None


class GrokCookies(BaseModel):
    """Authentication cookies for Grok API."""

    sso: str = Field(..., description="SSO JWT token")
    sso_rw: str = Field(..., alias="sso-rw", description="SSO read-write JWT token")
    x_userid: str = Field(..., alias="x-userid", description="User ID")
    cf_clearance: str = Field(..., description="Cloudflare clearance token")

    model_config = ConfigDict(populate_by_name=True)

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary for requests library."""
        return {
            "sso": self.sso,
            "sso-rw": self.sso_rw,
            "x-userid": self.x_userid,
            "cf_clearance": self.cf_clearance,
        }


class VideoMatchResult(BaseModel):
    """Result of matching a local video file to its web counterpart."""

    # Identifiers
    parent_id: str = Field(..., description="Parent post UUID")
    video_id: str = Field(..., description="Video UUID (child or parent for txt2vid)")
    is_parent_video: bool = Field(
        False, description="True if this is a txt2vid parent video (not a child)"
    )

    # Metadata
    mode: GenerationMode = Field(..., description="Generation mode")
    original_prompt: str | None = Field(None, description="Video generation prompt")
    file_size: int = Field(..., description="File size in bytes")

    # Generated filename
    new_filename: str = Field(..., description="New filename following naming convention")

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for this video's parent post."""
        return f"https://grok.com/imagine/post/{self.parent_id}"


class VideoGenerationResult(BaseModel):
    """Result of create_video_from_image() API call."""

    # Core identifiers
    video_id: str = Field(..., description="Generated video UUID")
    parent_post_id: str = Field(..., description="Parent image post UUID")

    # Generation status
    moderated: bool = Field(False, description="True if content was flagged by moderation")
    progress: int = Field(100, description="Generation progress (100 = complete)")

    # Metadata
    mode: str = Field("normal", description="Generation mode (normal, custom, etc.)")
    model_name: str | None = Field(None, description="Model used (e.g., imagine_xdit_1)")
    image_reference: str | None = Field(None, description="Source image URL")

    # Conversation info (for debugging)
    conversation_id: str | None = Field(None, description="Chat conversation UUID")

    # Style control (for MCTS pipeline)
    statsig_id: str | None = Field(
        None,
        description=(
            "Style seed (x-statsig-id) used for this generation. "
            "IMPORTANT: Same statsig_id produces ~99% similar video styles "
            "(camera motion, character movement, animation timing). "
            "Save this value to reproduce similar styles in future generations. "
            "Format: 94-char Base64 encoding 70 random bytes."
        ),
    )

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for the parent post (video will appear there)."""
        return f"https://grok.com/imagine/post/{self.parent_post_id}"

    @property
    def success(self) -> bool:
        """Check if video was generated successfully (not moderated)."""
        return self.progress == 100 and not self.moderated


class VideoExtendResult(BaseModel):
    """Result of extend_video() — extends a video with continuation frames."""

    video_id: str = Field(..., description="New extended video UUID")
    source_video_id: str = Field(..., description="Original video UUID that was extended")
    parent_post_id: str = Field(..., description="Parent image post UUID")
    moderated: bool = Field(False, description="True if content was flagged by moderation")
    progress: int = Field(100, description="Generation progress (100 = complete)")
    mode: str = Field("extend", description="Generation mode")
    model_name: str | None = Field(None, description="Model used")
    conversation_id: str | None = Field(None, description="Chat conversation UUID")
    statsig_id: str | None = Field(
        None,
        description="Style seed (x-statsig-id) used for this generation",
    )

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for the extended video's parent post."""
        return f"https://grok.com/imagine/post/{self.source_video_id}"

    @property
    def success(self) -> bool:
        """Check if video was extended successfully."""
        return self.progress == 100 and not self.moderated


class ImageEditResult(BaseModel):
    """Result of edit_image_via_ui() API call."""

    # Source info
    post_id: str = Field(..., description="Original post UUID that was edited")
    edit_prompt: str = Field(..., description="Edit prompt used")

    # Generated images (each with id, url, moderated status)
    images: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of generated images with keys: image_id, image_url, moderated, r_rated",
    )

    # Conversation info (for debugging)
    conversation_id: str | None = Field(None, description="Chat conversation UUID")

    @computed_field
    @property
    def image_urls(self) -> list[str]:
        """URLs of successfully generated (non-moderated) images."""
        urls = []
        for img in self.images:
            if img.get("moderated") or not img.get("image_url"):
                continue
            url = img["image_url"]
            # Handle both full URLs and relative paths
            if url.startswith("http"):
                urls.append(url)
            else:
                urls.append(f"https://assets.grok.com/{url}")
        return urls

    @computed_field
    @property
    def moderated_count(self) -> int:
        """Number of images that were moderated."""
        return sum(1 for img in self.images if img.get("moderated"))

    @computed_field
    @property
    def r_rated_count(self) -> int:
        """Number of images flagged as R-rated (adult content)."""
        return sum(1 for img in self.images if img.get("r_rated"))

    @computed_field
    @property
    def success_count(self) -> int:
        """Number of successfully generated (non-moderated) images."""
        return len(self.images) - self.moderated_count

    @computed_field
    @property
    def total_count(self) -> int:
        """Total images generated (successful + moderated)."""
        return len(self.images)

    def has_enough_success(self, min_count: int = 1) -> bool:
        """Check if at least min_count images were generated successfully."""
        return self.success_count >= min_count

    @property
    def success(self) -> bool:
        """Check if at least one image was generated successfully."""
        return self.success_count > 0

    @computed_field
    @property
    def post_ids(self) -> list[str]:
        """Post IDs of successfully generated images (for saving via favorite_post())."""
        return [
            img["post_id"] for img in self.images if not img.get("moderated") and img.get("post_id")
        ]


class ImageGenerationResult(BaseModel):
    """Result of create_image() API call (text-to-image generation).

    IMPORTANT: Generated images are temporary and NOT automatically saved.
    The gallery disappears on page refresh. To persist an image, you must
    manually favorite/save it using favorite_post() with the post_id.
    """

    # Source info
    prompt: str = Field(..., description="Text prompt used for generation")

    # Generated images (each with id, url, moderated status, r_rated, etc.)
    images: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of generated images with keys: image_id, image_url, moderated, r_rated",
    )

    # Conversation info (for debugging)
    conversation_id: str | None = Field(None, description="Chat conversation UUID")

    # Post IDs collected via thumbnail_selector callback
    selected_post_ids: list[str] = Field(
        default_factory=list,
        description="Post IDs of images selected via thumbnail_selector callback",
    )

    @computed_field
    @property
    def image_urls(self) -> list[str]:
        """URLs of successfully generated (non-moderated) images."""
        urls = []
        for img in self.images:
            if img.get("moderated") or not img.get("image_url"):
                continue
            url = img["image_url"]
            # Handle both full URLs and relative paths
            if url.startswith("http"):
                urls.append(url)
            else:
                urls.append(f"https://assets.grok.com/{url}")
        return urls

    @computed_field
    @property
    def moderated_count(self) -> int:
        """Number of images that were moderated."""
        return sum(1 for img in self.images if img.get("moderated"))

    @computed_field
    @property
    def r_rated_count(self) -> int:
        """Number of images flagged as R-rated (adult content)."""
        return sum(1 for img in self.images if img.get("r_rated"))

    @computed_field
    @property
    def success_count(self) -> int:
        """Number of successfully generated (non-moderated) images."""
        return len(self.images) - self.moderated_count

    @computed_field
    @property
    def total_count(self) -> int:
        """Total images generated (successful + moderated)."""
        return len(self.images)

    def has_enough_success(self, min_count: int = 1) -> bool:
        """Check if at least min_count images were generated successfully."""
        return self.success_count >= min_count

    @property
    def success(self) -> bool:
        """Check if at least one image was generated successfully."""
        return self.success_count > 0

    @computed_field
    @property
    def post_ids(self) -> list[str]:
        """Post IDs of successfully generated images (for saving via favorite_post())."""
        return [
            img["post_id"] for img in self.images if not img.get("moderated") and img.get("post_id")
        ]
