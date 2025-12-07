"""Pydantic models for Grok API responses."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field


class GrokVideo(BaseModel):
    """Represents a video generated from a Grok Imagine post (childPost)."""

    id: str = Field(..., description="Video UUID")
    original_post_id: str = Field(..., description="Parent image post UUID")
    prompt: str | None = Field(None, description="Video generation prompt")
    media_url: str | None = Field(None, description="Standard video URL")
    hd_media_url: str | None = Field(None, description="HD video URL")
    thumbnail_url: str | None = Field(None, description="Thumbnail image URL")
    created_at: datetime | None = Field(None, description="Creation timestamp")
    duration: int | None = Field(None, description="Video duration in seconds")
    model_name: str | None = Field(None, description="Model used for generation")
    resolution: dict[str, int] | None = Field(None, description="Video resolution")

    @computed_field
    @property
    def url(self) -> str:
        """Web URL for the parent post (videos share parent's URL)."""
        return f"https://grok.com/imagine/post/{self.original_post_id}"


class GrokPost(BaseModel):
    """Represents a Grok Imagine post (image with optional video children)."""

    id: str = Field(..., description="Post UUID")
    user_id: str | None = Field(None, description="User UUID")
    prompt: str | None = Field(None, description="Original generation prompt")
    media_type: str | None = Field(None, description="MEDIA_POST_TYPE_IMAGE or MEDIA_POST_TYPE_VIDEO")
    media_url: str | None = Field(None, description="Image/video URL")
    thumbnail_url: str | None = Field(None, description="Thumbnail URL")
    created_at: datetime | None = Field(None, description="Creation timestamp")
    model_name: str | None = Field(None, description="Model used for generation")
    resolution: dict[str, int] | None = Field(None, description="Media resolution")
    videos: list[GrokVideo] = Field(default_factory=list, description="Child video posts")
    raw_data: dict[str, Any] | None = Field(None, description="Raw API response")

    @computed_field
    @property
    def url(self) -> str:
        """Web URL for this post."""
        return f"https://grok.com/imagine/post/{self.id}"

    @computed_field
    @property
    def video_filename(self) -> str:
        """Expected local filename pattern for videos from this post."""
        return f"grok-video-{self.id}.mp4"

    @property
    def has_videos(self) -> bool:
        """Check if this post has any video children."""
        return len(self.videos) > 0

    @property
    def latest_video(self) -> GrokVideo | None:
        """Get the most recently created video, if any."""
        if not self.videos:
            return None
        return max(self.videos, key=lambda v: v.created_at or datetime.min)


class GrokCookies(BaseModel):
    """Authentication cookies for Grok API."""

    sso: str = Field(..., description="SSO JWT token")
    sso_rw: str = Field(..., alias="sso-rw", description="SSO read-write JWT token")
    x_userid: str = Field(..., alias="x-userid", description="User ID")
    cf_clearance: str = Field(..., description="Cloudflare clearance token")

    def to_cookie_dict(self) -> dict[str, str]:
        """Convert to dictionary suitable for requests library."""
        return {
            "sso": self.sso,
            "sso-rw": self.sso_rw,
            "x-userid": self.x_userid,
            "cf_clearance": self.cf_clearance,
        }

    class Config:
        populate_by_name = True  # Allow both 'sso_rw' and 'sso-rw'
