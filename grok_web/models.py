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


class ChildVideo(BaseModel):
    """A video generated from a parent post (appears in childPosts array)."""

    id: str = Field(..., description="Child video UUID")
    parent_id: str = Field(..., description="Parent post UUID")

    # Prompts
    original_prompt: str | None = Field(None, description="Video generation/edit prompt")

    # URLs
    media_url: str | None = Field(None, description="Standard quality video URL")
    hd_media_url: str | None = Field(None, description="HD video URL")
    thumbnail_url: str | None = Field(None, description="Thumbnail image URL")

    # Metadata
    created_at: datetime | None = Field(None, description="Creation timestamp (UTC)")
    resolution: dict[str, int] | None = Field(None, description="Video resolution {width, height}")
    duration: int | None = Field(None, description="Video duration in milliseconds")
    model_name: str | None = Field(None, description="Model used (e.g., imagine_h_1)")
    mode: str | None = Field(None, description="Generation mode: 'custom' or 'text'")

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL (videos share parent's URL)."""
        return f"https://grok.com/imagine/post/{self.parent_id}"

    @computed_field
    @property
    def best_video_url(self) -> str | None:
        """Best available video URL (HD preferred)."""
        return self.hd_media_url or self.media_url


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

    # Child videos
    children: list[ChildVideo] = Field(default_factory=list, description="Child video posts")

    # Raw data for debugging
    raw_data: dict[str, Any] | None = Field(None, description="Raw API response")

    @computed_field
    @property
    def web_url(self) -> str:
        """Web URL for this post."""
        return f"https://grok.com/imagine/post/{self.id}"

    @computed_field
    @property
    def video_count(self) -> int:
        """Number of child videos."""
        return len(self.children)

    @property
    def has_children(self) -> bool:
        """Check if this post has any child videos."""
        return len(self.children) > 0


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
