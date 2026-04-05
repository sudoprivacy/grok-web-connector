"""Tests for ResponseParser class."""

from datetime import datetime, timezone

import pytest

from grok_web._internal import ResponseParser
from grok_web.models import GenerationMode, PostDetails, PostSummary


class TestResponseParser:
    """Tests for ResponseParser parsing methods."""

    @pytest.fixture
    def parser(self) -> ResponseParser:
        """Create a ResponseParser instance."""
        return ResponseParser()

    # =========================================================================
    # _detect_generation_mode tests
    # =========================================================================

    def test_detect_mode_text_to_video(self, parser: ResponseParser):
        """Text-to-video: mediaType=VIDEO, mode=text."""
        data = {"mediaType": "MEDIA_POST_TYPE_VIDEO", "mode": "text"}
        assert parser._detect_generation_mode(data) == GenerationMode.TEXT_TO_VIDEO

    def test_detect_mode_grok_image_to_video(self, parser: ResponseParser):
        """Grok image-to-video: mediaType=IMAGE, has prompt."""
        data = {"mediaType": "MEDIA_POST_TYPE_IMAGE", "prompt": "some prompt"}
        assert parser._detect_generation_mode(data) == GenerationMode.GROK_IMAGE_TO_VIDEO

    def test_detect_mode_upload_image_to_video(self, parser: ResponseParser):
        """Upload image-to-video: mediaType=IMAGE, no prompt."""
        data = {"mediaType": "MEDIA_POST_TYPE_IMAGE", "prompt": None}
        assert parser._detect_generation_mode(data) == GenerationMode.UPLOAD_IMAGE_TO_VIDEO

    def test_detect_mode_upload_image_empty_prompt(self, parser: ResponseParser):
        """Upload image-to-video: mediaType=IMAGE, empty string prompt."""
        data = {"mediaType": "MEDIA_POST_TYPE_IMAGE", "prompt": ""}
        assert parser._detect_generation_mode(data) == GenerationMode.UPLOAD_IMAGE_TO_VIDEO

    def test_detect_mode_video_unknown(self, parser: ResponseParser):
        """Unknown video mode: mediaType=VIDEO, mode not text."""
        data = {"mediaType": "MEDIA_POST_TYPE_VIDEO", "mode": "normal"}
        assert parser._detect_generation_mode(data) == GenerationMode.UNKNOWN

    def test_detect_mode_unknown_media_type(self, parser: ResponseParser):
        """Unknown mode: unrecognized mediaType."""
        data = {"mediaType": "MEDIA_POST_TYPE_AUDIO"}
        assert parser._detect_generation_mode(data) == GenerationMode.UNKNOWN

    def test_detect_mode_empty_data(self, parser: ResponseParser):
        """Unknown mode: empty data."""
        assert parser._detect_generation_mode({}) == GenerationMode.UNKNOWN

    # =========================================================================
    # _parse_timestamp tests
    # =========================================================================

    def test_parse_timestamp_iso_with_z(self, parser: ResponseParser):
        """Parse ISO timestamp with Z suffix."""
        result = parser._parse_timestamp("2025-12-10T10:30:00Z")
        assert result == datetime(2025, 12, 10, 10, 30, 0, tzinfo=timezone.utc)

    def test_parse_timestamp_iso_with_offset(self, parser: ResponseParser):
        """Parse ISO timestamp with timezone offset."""
        result = parser._parse_timestamp("2025-12-10T10:30:00+00:00")
        assert result == datetime(2025, 12, 10, 10, 30, 0, tzinfo=timezone.utc)

    def test_parse_timestamp_with_microseconds(self, parser: ResponseParser):
        """Parse ISO timestamp with microseconds."""
        result = parser._parse_timestamp("2025-12-10T10:30:00.123456Z")
        assert result is not None
        assert result.year == 2025
        assert result.month == 12
        assert result.day == 10

    def test_parse_timestamp_none(self, parser: ResponseParser):
        """Return None for None input."""
        assert parser._parse_timestamp(None) is None

    def test_parse_timestamp_empty_string(self, parser: ResponseParser):
        """Return None for empty string."""
        assert parser._parse_timestamp("") is None

    def test_parse_timestamp_invalid(self, parser: ResponseParser):
        """Return None for invalid timestamp."""
        assert parser._parse_timestamp("not-a-timestamp") is None

    def test_parse_timestamp_already_datetime(self, parser: ResponseParser):
        """Return datetime if already datetime."""
        dt = datetime(2025, 12, 10, 10, 30, 0, tzinfo=timezone.utc)
        assert parser._parse_timestamp(dt) == dt

    # =========================================================================
    # _parse_post_summary tests
    # =========================================================================

    def test_parse_post_summary_basic(self, parser: ResponseParser, sample_post_data: dict):
        """Parse basic post summary."""
        summary = parser._parse_post_summary(sample_post_data)

        assert isinstance(summary, PostSummary)
        assert summary.id == "test-post-id-1234"
        assert summary.mode == GenerationMode.GROK_IMAGE_TO_VIDEO
        assert summary.prompt_preview == "A beautiful sunset over the ocean"
        assert summary.video_count == 2
        assert summary.media_type == "MEDIA_POST_TYPE_IMAGE"
        assert summary.created_at is not None

    def test_parse_post_summary_truncates_long_prompt(self, parser: ResponseParser):
        """Prompt preview is truncated to 100 chars."""
        long_prompt = "A" * 200
        data = {
            "id": "test-id",
            "mediaType": "MEDIA_POST_TYPE_IMAGE",
            "prompt": long_prompt,
            "childPosts": [],
        }
        summary = parser._parse_post_summary(data)
        assert len(summary.prompt_preview) == 100

    def test_parse_post_summary_no_prompt(self, parser: ResponseParser):
        """Handle post with no prompt."""
        data = {
            "id": "test-id",
            "mediaType": "MEDIA_POST_TYPE_IMAGE",
            "childPosts": [],
        }
        summary = parser._parse_post_summary(data)
        assert summary.prompt_preview is None

    def test_parse_post_summary_uses_original_prompt(self, parser: ResponseParser):
        """Falls back to originalPrompt if prompt missing."""
        data = {
            "id": "test-id",
            "mediaType": "MEDIA_POST_TYPE_IMAGE",
            "originalPrompt": "Original prompt text",
            "childPosts": [],
        }
        summary = parser._parse_post_summary(data)
        assert summary.prompt_preview == "Original prompt text"

    def test_parse_post_summary_counts_only_videos(self, parser: ResponseParser):
        """Video count only includes VIDEO type children."""
        data = {
            "id": "test-id",
            "mediaType": "MEDIA_POST_TYPE_IMAGE",
            "childPosts": [
                {"mediaType": "MEDIA_POST_TYPE_VIDEO"},
                {"mediaType": "MEDIA_POST_TYPE_VIDEO"},
                {"mediaType": "MEDIA_POST_TYPE_IMAGE"},  # Not counted
            ],
        }
        summary = parser._parse_post_summary(data)
        assert summary.video_count == 2

    # =========================================================================
    # _parse_post_details tests
    # =========================================================================

    def test_parse_post_details_full(self, parser: ResponseParser, sample_post_data: dict):
        """Parse full post details with children."""
        details = parser._parse_post_details(sample_post_data, "test-post-id-1234")

        assert isinstance(details, PostDetails)
        assert details.id == "test-post-id-1234"
        assert details.user_id == "user-123"
        assert details.mode == GenerationMode.GROK_IMAGE_TO_VIDEO
        assert details.prompt == "A beautiful sunset over the ocean"
        assert details.media_url == "https://assets.grok.com/image.jpg"
        assert details.hd_media_url == "https://assets.grok.com/image_hd.jpg"
        assert details.resolution == {"width": 1920, "height": 1080}
        assert details.model_name == "aurora"

        # Check children (2 videos + 1 image)
        assert len(details.children) == 3
        child1 = details.children[0]
        assert child1.id == "child-video-id-1"
        assert child1.media_type == "MEDIA_POST_TYPE_VIDEO"
        assert child1.original_post_id == "test-post-id-1234"
        assert child1.original_prompt == "Make it move"
        assert child1.duration == 6

        # Image child is also included
        child3 = details.children[2]
        assert child3.id == "child-image-id-1"
        assert child3.media_type == "MEDIA_POST_TYPE_IMAGE"
        assert child3.original_post_id == "test-post-id-1234"

    def test_parse_post_details_with_raw_data(self, parser: ResponseParser, sample_post_data: dict):
        """Raw data is preserved when passed."""
        raw = {"post": sample_post_data, "extra": "field"}
        details = parser._parse_post_details(sample_post_data, "test-id", raw_data=raw)
        assert details.raw_data == raw

    def test_parse_post_details_no_children(
        self, parser: ResponseParser, sample_text_to_video_post: dict
    ):
        """Handle post with no children."""
        details = parser._parse_post_details(sample_text_to_video_post, "txt2vid-post-id")
        assert details.children == []

    def test_parse_post_details_includes_image_and_video_children(self, parser: ResponseParser):
        """Both image and video children are included."""
        data = {
            "id": "test-id",
            "mediaType": "MEDIA_POST_TYPE_IMAGE",
            "childPosts": [
                {"id": "video1", "mediaType": "MEDIA_POST_TYPE_VIDEO"},
                {"id": "image1", "mediaType": "MEDIA_POST_TYPE_IMAGE"},
                {"id": "audio1", "mediaType": "MEDIA_POST_TYPE_AUDIO"},  # Filtered
            ],
        }
        details = parser._parse_post_details(data, "test-id")
        assert len(details.children) == 2
        assert details.children[0].id == "video1"
        assert details.children[1].id == "image1"

    def test_parse_post_details_uses_fallback_id(self, parser: ResponseParser):
        """Uses provided post_id as fallback."""
        data = {"mediaType": "MEDIA_POST_TYPE_IMAGE", "childPosts": []}
        details = parser._parse_post_details(data, "fallback-id")
        assert details.id == "fallback-id"

    def test_parse_post_details_child_uses_original_post_id_fallback(self, parser: ResponseParser):
        """Child uses post_id if originalPostId missing."""
        data = {
            "id": "parent-id",
            "mediaType": "MEDIA_POST_TYPE_IMAGE",
            "childPosts": [
                {"id": "child-id", "mediaType": "MEDIA_POST_TYPE_VIDEO"},
            ],
        }
        details = parser._parse_post_details(data, "parent-id")
        assert details.children[0].original_post_id == "parent-id"
