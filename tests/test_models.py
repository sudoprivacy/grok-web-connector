"""Tests for data models."""

from datetime import datetime, timezone

from grok_web.models import (
    ChildVideo,
    GenerationMode,
    GrokCookies,
    PostDetails,
    PostSummary,
    VideoMatchResult,
    VideoPreset,
)


class TestGrokCookies:
    """Tests for GrokCookies dataclass."""

    def test_create_with_required_fields(self):
        """Create with all required fields."""
        cookies = GrokCookies(
            sso="sso_token",
            **{"sso-rw": "sso_rw_token"},
            **{"x-userid": "user-123"},
            cf_clearance="cf_token",
        )
        assert cookies.sso == "sso_token"
        assert cookies.sso_rw == "sso_rw_token"
        assert cookies.cf_clearance == "cf_token"
        assert cookies.x_userid == "user-123"

    def test_to_dict(self):
        """to_dict returns dictionary with all cookies."""
        cookies = GrokCookies(
            sso="sso_token",
            **{"sso-rw": "sso_rw_token"},
            **{"x-userid": "user-123"},
            cf_clearance="cf_token",
        )
        result = cookies.to_dict()

        assert isinstance(result, dict)
        assert result["sso"] == "sso_token"
        assert result["sso-rw"] == "sso_rw_token"
        assert result["cf_clearance"] == "cf_token"
        assert result["x-userid"] == "user-123"

    def test_to_dict_key_format(self):
        """to_dict uses correct key names (sso-rw not sso_rw)."""
        cookies = GrokCookies(
            sso="a",
            **{"sso-rw": "b"},
            **{"x-userid": "c"},
            cf_clearance="d",
        )
        result = cookies.to_dict()

        # Key should be "sso-rw" not "sso_rw"
        assert "sso-rw" in result
        assert "x-userid" in result


class TestGenerationMode:
    """Tests for GenerationMode enum."""

    def test_enum_values(self):
        """All expected enum values exist."""
        assert GenerationMode.TEXT_TO_VIDEO.value == "txt2vid"
        assert GenerationMode.GROK_IMAGE_TO_VIDEO.value == "img2vid"
        assert GenerationMode.UPLOAD_IMAGE_TO_VIDEO.value == "upload2vid"
        assert GenerationMode.TEXT_TO_IMAGE.value == "txt2img"
        assert GenerationMode.UNKNOWN.value == "unknown"


class TestPostSummary:
    """Tests for PostSummary dataclass."""

    def test_create_minimal(self):
        """Create with minimal required fields."""
        summary = PostSummary(
            id="test-id",
            mode=GenerationMode.TEXT_TO_VIDEO,
        )
        assert summary.id == "test-id"
        assert summary.mode == GenerationMode.TEXT_TO_VIDEO
        assert summary.prompt_preview is None
        assert summary.video_count == 0

    def test_create_full(self):
        """Create with all fields."""
        created = datetime(2025, 12, 10, 10, 30, 0, tzinfo=timezone.utc)
        summary = PostSummary(
            id="test-id",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            prompt_preview="A beautiful sunset",
            video_count=3,
            created_at=created,
            media_type="MEDIA_POST_TYPE_IMAGE",
        )
        assert summary.prompt_preview == "A beautiful sunset"
        assert summary.video_count == 3
        assert summary.created_at == created
        assert summary.media_type == "MEDIA_POST_TYPE_IMAGE"

    def test_web_url_computed(self):
        """web_url is correctly computed."""
        summary = PostSummary(id="test-id-123", mode=GenerationMode.TEXT_TO_VIDEO)
        assert summary.web_url == "https://grok.com/imagine/post/test-id-123"


class TestPostDetails:
    """Tests for PostDetails dataclass."""

    def test_create_minimal(self):
        """Create with minimal required fields."""
        details = PostDetails(id="test-id", mode=GenerationMode.TEXT_TO_VIDEO)
        assert details.id == "test-id"
        assert details.children == []
        assert details.raw_data is None

    def test_create_with_children(self):
        """Create with child videos."""
        child = ChildVideo(id="child-1", parent_id="test-id")
        details = PostDetails(
            id="test-id",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            children=[child],
        )
        assert len(details.children) == 1
        assert details.children[0].id == "child-1"

    def test_all_optional_fields(self):
        """All optional fields can be set."""
        details = PostDetails(
            id="test-id",
            mode=GenerationMode.TEXT_TO_VIDEO,
            user_id="user-123",
            media_type="MEDIA_POST_TYPE_VIDEO",
            prompt="A prompt",
            original_prompt="Original prompt",
            media_url="https://example.com/video.mp4",
            hd_media_url="https://example.com/video_hd.mp4",
            thumbnail_url="https://example.com/thumb.jpg",
            created_at=datetime.now(timezone.utc),
            resolution={"width": 1920, "height": 1080},
            model_name="mochi",
            raw_data={"key": "value"},
        )
        assert details.user_id == "user-123"
        assert details.media_url == "https://example.com/video.mp4"
        assert details.resolution == {"width": 1920, "height": 1080}

    def test_video_count_computed(self):
        """video_count is computed from children."""
        child1 = ChildVideo(id="child-1", parent_id="test-id")
        child2 = ChildVideo(id="child-2", parent_id="test-id")
        details = PostDetails(
            id="test-id",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            children=[child1, child2],
        )
        assert details.video_count == 2

    def test_has_children(self):
        """has_children property works correctly."""
        details_no_children = PostDetails(id="test-1", mode=GenerationMode.TEXT_TO_VIDEO)
        assert details_no_children.has_children is False

        child = ChildVideo(id="child-1", parent_id="test-2")
        details_with_children = PostDetails(
            id="test-2",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            children=[child],
        )
        assert details_with_children.has_children is True


class TestChildVideo:
    """Tests for ChildVideo dataclass."""

    def test_create_minimal(self):
        """Create with minimal required fields."""
        child = ChildVideo(id="child-1", parent_id="parent-1")
        assert child.id == "child-1"
        assert child.parent_id == "parent-1"

    def test_create_full(self):
        """Create with all fields."""
        child = ChildVideo(
            id="child-1",
            parent_id="parent-1",
            original_prompt="Make it move",
            media_url="https://example.com/video.mp4",
            hd_media_url="https://example.com/video_hd.mp4",
            thumbnail_url="https://example.com/thumb.jpg",
            created_at=datetime.now(timezone.utc),
            resolution={"width": 1920, "height": 1080},
            duration=6000,
            model_name="mochi",
            mode="normal",
        )
        assert child.duration == 6000
        assert child.mode == "normal"
        assert child.resolution == {"width": 1920, "height": 1080}

    def test_web_url_computed(self):
        """web_url returns direct URL to this video."""
        child = ChildVideo(id="child-1", parent_id="parent-123")
        assert child.web_url == "https://grok.com/imagine/post/child-1"

    def test_parent_web_url_computed(self):
        """parent_web_url returns URL to parent post."""
        child = ChildVideo(id="child-1", parent_id="parent-123")
        assert child.parent_web_url == "https://grok.com/imagine/post/parent-123"

    def test_best_video_url_prefers_hd(self):
        """best_video_url prefers HD URL."""
        child = ChildVideo(
            id="child-1",
            parent_id="parent-1",
            media_url="https://example.com/video.mp4",
            hd_media_url="https://example.com/video_hd.mp4",
        )
        assert child.best_video_url == "https://example.com/video_hd.mp4"

    def test_best_video_url_falls_back(self):
        """best_video_url falls back to media_url."""
        child = ChildVideo(
            id="child-1",
            parent_id="parent-1",
            media_url="https://example.com/video.mp4",
        )
        assert child.best_video_url == "https://example.com/video.mp4"


class TestVideoMatchResult:
    """Tests for VideoMatchResult dataclass."""

    def test_create_full(self):
        """Create with all fields."""
        result = VideoMatchResult(
            parent_id="parent-123",
            video_id="video-456",
            is_parent_video=False,
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            original_prompt="A sunset",
            file_size=1234567,
            new_filename="grok-video_parent-123_video-456.mp4",
        )
        assert result.parent_id == "parent-123"
        assert result.video_id == "video-456"
        assert result.is_parent_video is False
        assert result.file_size == 1234567
        assert "parent-123" in result.new_filename
        assert "video-456" in result.new_filename

    def test_web_url_computed(self):
        """web_url is correctly computed."""
        result = VideoMatchResult(
            parent_id="parent-123",
            video_id="video-456",
            mode=GenerationMode.TEXT_TO_VIDEO,
            file_size=1000,
            new_filename="test.mp4",
        )
        assert result.web_url == "https://grok.com/imagine/post/parent-123"


class TestVideoPreset:
    """Tests for VideoPreset enum."""

    def test_enum_values(self):
        """All expected enum values exist with correct API mode values."""
        assert VideoPreset.NORMAL.value == "normal"
        assert VideoPreset.FUN.value == "extremely-crazy"
        assert VideoPreset.SPICY.value == "extremely-spicy-or-crazy"

    def test_enum_value_is_string(self):
        """VideoPreset enum values are strings."""
        # VideoPreset.value returns the API mode string
        assert VideoPreset.NORMAL.value == "normal"
        assert VideoPreset.FUN.value == "extremely-crazy"
        assert VideoPreset.SPICY.value == "extremely-spicy-or-crazy"
        # Values are actual string instances
        assert isinstance(VideoPreset.NORMAL.value, str)
        assert isinstance(VideoPreset.FUN.value, str)

    def test_enum_comparison(self):
        """VideoPreset can be compared with strings."""
        assert VideoPreset.NORMAL == "normal"
        assert VideoPreset.FUN == "extremely-crazy"
        assert VideoPreset.SPICY == "extremely-spicy-or-crazy"

    def test_enum_from_string(self):
        """VideoPreset can be created from string value."""
        assert VideoPreset("normal") == VideoPreset.NORMAL
        assert VideoPreset("extremely-crazy") == VideoPreset.FUN
        assert VideoPreset("extremely-spicy-or-crazy") == VideoPreset.SPICY

    def test_enum_names(self):
        """VideoPreset enum names are user-friendly."""
        assert VideoPreset.NORMAL.name == "NORMAL"
        assert VideoPreset.FUN.name == "FUN"
        assert VideoPreset.SPICY.name == "SPICY"


# Note: TestVideoPresetMapping was removed as redundant.
# The actual resolve_preset() function is tested in test_internal_utilities.py
