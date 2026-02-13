"""Tests for ResponseParser helper methods from _internal.py.

These tests cover the pure helper methods that don't involve I/O.
Testing internal methods directly provides better coverage and isolation.
"""

from datetime import datetime, timezone

import pytest

from grok_web._internal import ResponseParser
from grok_web.exceptions import GrokAPIError
from grok_web.models import GenerationMode, PostDetails


class TestResponseParserHelpers:
    """Tests for ResponseParser helper methods."""

    @pytest.fixture
    def parser(self) -> ResponseParser:
        """Create ResponseParser instance."""
        return ResponseParser()

    # =========================================================================
    # _validate_asset_url tests
    # =========================================================================

    def test_validate_asset_url_valid_assets_grok_com(self, parser: ResponseParser):
        """Valid assets.grok.com URL passes validation."""
        url = "https://assets.grok.com/media/abc123.mp4"
        parser._validate_asset_url(url)  # Should not raise

    def test_validate_asset_url_valid_imagine_public(self, parser: ResponseParser):
        """Valid imagine-public.x.ai URL passes validation."""
        url = "https://imagine-public.x.ai/imagine-public/share-videos/abc123.mp4"
        parser._validate_asset_url(url)  # Should not raise

    def test_validate_asset_url_empty_raises(self, parser: ResponseParser):
        """Empty URL raises GrokAPIError."""
        with pytest.raises(GrokAPIError, match="Asset URL is empty"):
            parser._validate_asset_url("")

    def test_validate_asset_url_none_raises(self, parser: ResponseParser):
        """None URL raises GrokAPIError."""
        with pytest.raises(GrokAPIError, match="Asset URL is empty"):
            parser._validate_asset_url(None)

    def test_validate_asset_url_invalid_domain_raises(self, parser: ResponseParser):
        """Invalid domain raises GrokAPIError."""
        with pytest.raises(GrokAPIError, match="Invalid asset URL"):
            parser._validate_asset_url("https://example.com/video.mp4")

    def test_validate_asset_url_http_instead_of_https_raises(self, parser: ResponseParser):
        """HTTP instead of HTTPS raises GrokAPIError."""
        with pytest.raises(GrokAPIError, match="Invalid asset URL"):
            parser._validate_asset_url("http://assets.grok.com/media/abc123.mp4")

    # =========================================================================
    # _extract_parent_info tests
    # =========================================================================

    def test_extract_parent_info_child_video(self, parser: ResponseParser):
        """Extract parent info from child video (img2vid)."""
        details = PostDetails(
            id="child-video-id",
            user_id="user-123",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            created_at=datetime.now(timezone.utc),
            raw_data={"post": {"originalPostId": "parent-post-id"}},
        )

        parent_id, is_parent = parser._extract_parent_info(details, "child-video-id")

        assert parent_id == "parent-post-id"
        assert is_parent is False

    def test_extract_parent_info_parent_video(self, parser: ResponseParser):
        """Extract parent info from parent video (txt2vid)."""
        details = PostDetails(
            id="parent-video-id",
            user_id="user-123",
            mode=GenerationMode.TEXT_TO_VIDEO,
            created_at=datetime.now(timezone.utc),
            raw_data={"post": {"originalPostId": "parent-video-id"}},
        )

        parent_id, is_parent = parser._extract_parent_info(details, "parent-video-id")

        assert parent_id == "parent-video-id"
        assert is_parent is True

    def test_extract_parent_info_no_original_post_id(self, parser: ResponseParser):
        """Extract parent info when originalPostId is missing."""
        details = PostDetails(
            id="video-id",
            user_id="user-123",
            mode=GenerationMode.TEXT_TO_VIDEO,
            created_at=datetime.now(timezone.utc),
            raw_data={"post": {}},
        )

        parent_id, is_parent = parser._extract_parent_info(details, "video-id")

        assert parent_id == "video-id"
        assert is_parent is True

    def test_extract_parent_info_no_raw_data(self, parser: ResponseParser):
        """Extract parent info when raw_data is None."""
        details = PostDetails(
            id="video-id",
            user_id="user-123",
            mode=GenerationMode.TEXT_TO_VIDEO,
            created_at=datetime.now(timezone.utc),
            raw_data=None,
        )

        parent_id, is_parent = parser._extract_parent_info(details, "video-id")

        assert parent_id == "video-id"
        assert is_parent is True

    # =========================================================================
    # _verify_file_size_match tests
    # =========================================================================

    def test_verify_file_size_match_success(self, parser: ResponseParser):
        """Matching file sizes pass verification."""
        parser._verify_file_size_match(
            video_id="video-123", filename="video.mp4", local_size=1024, web_size=1024
        )  # Should not raise

    def test_verify_file_size_match_mismatch_raises(self, parser: ResponseParser):
        """Mismatching file sizes raise GrokAPIError."""
        with pytest.raises(GrokAPIError, match="File size mismatch"):
            parser._verify_file_size_match(
                video_id="video-123",
                filename="video.mp4",
                local_size=1024,
                web_size=2048,
            )

    def test_verify_file_size_match_error_includes_details(self, parser: ResponseParser):
        """Error message includes video_id, filename, and sizes."""
        with pytest.raises(GrokAPIError) as exc_info:
            parser._verify_file_size_match(
                video_id="test-video",
                filename="test.mp4",
                local_size=100,
                web_size=200,
            )

        error_msg = str(exc_info.value)
        assert "test-video" in error_msg
        assert "test.mp4" in error_msg
        assert "100" in error_msg
        assert "200" in error_msg

    # =========================================================================
    # _build_video_match_result tests
    # =========================================================================

    def test_build_video_match_result_child_video(self, parser: ResponseParser):
        """Build VideoMatchResult for child video."""
        details = PostDetails(
            id="video-id",
            user_id="user-123",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            original_prompt="Orbit camera",
            created_at=datetime.now(timezone.utc),
        )

        result = parser._build_video_match_result(
            parent_id="parent-123",
            video_id="video-456",
            is_parent_video=False,
            details=details,
            local_size=12345,
        )

        assert result.parent_id == "parent-123"
        assert result.video_id == "video-456"
        assert result.is_parent_video is False
        assert result.mode == GenerationMode.GROK_IMAGE_TO_VIDEO
        assert result.original_prompt == "Orbit camera"
        assert result.file_size == 12345
        assert result.new_filename == "grok-video_parent-123_video-456.mp4"

    def test_build_video_match_result_parent_video(self, parser: ResponseParser):
        """Build VideoMatchResult for parent video (txt2vid)."""
        details = PostDetails(
            id="parent-video-id",
            user_id="user-123",
            mode=GenerationMode.TEXT_TO_VIDEO,
            original_prompt="A cat playing",
            created_at=datetime.now(timezone.utc),
        )

        result = parser._build_video_match_result(
            parent_id="parent-123",
            video_id="parent-123",
            is_parent_video=True,
            details=details,
            local_size=54321,
        )

        assert result.parent_id == "parent-123"
        assert result.video_id == "parent-123"
        assert result.is_parent_video is True
        assert result.mode == GenerationMode.TEXT_TO_VIDEO
        assert result.file_size == 54321
        assert result.new_filename == "grok-video_parent-123_parent-123.mp4"

    # =========================================================================
    # _extract_media_url tests
    # =========================================================================

    def test_extract_media_url_prefers_hd(self, parser: ResponseParser):
        """Prefer HD media URL when available."""
        details = PostDetails(
            id="video-id",
            user_id="user-123",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            created_at=datetime.now(timezone.utc),
            media_url="https://example.com/video.mp4",
            hd_media_url="https://example.com/video_hd.mp4",
        )

        url = parser._extract_media_url(details, "video-id", "test.mp4")

        assert url == "https://example.com/video_hd.mp4"

    def test_extract_media_url_falls_back_to_sd(self, parser: ResponseParser):
        """Fall back to SD media URL when HD not available."""
        details = PostDetails(
            id="video-id",
            user_id="user-123",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            created_at=datetime.now(timezone.utc),
            media_url="https://example.com/video.mp4",
            hd_media_url=None,
        )

        url = parser._extract_media_url(details, "video-id", "test.mp4")

        assert url == "https://example.com/video.mp4"

    def test_extract_media_url_no_url_raises(self, parser: ResponseParser):
        """Raise GrokAPIError when no media URL available."""
        details = PostDetails(
            id="video-id",
            user_id="user-123",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            created_at=datetime.now(timezone.utc),
            media_url=None,
            hd_media_url=None,
        )

        with pytest.raises(GrokAPIError, match="No media URL found"):
            parser._extract_media_url(details, "video-id", "test.mp4")

    def test_extract_media_url_error_includes_details(self, parser: ResponseParser):
        """Error message includes video_id and filename."""
        details = PostDetails(
            id="video-id",
            user_id="user-123",
            mode=GenerationMode.GROK_IMAGE_TO_VIDEO,
            created_at=datetime.now(timezone.utc),
        )

        with pytest.raises(GrokAPIError) as exc_info:
            parser._extract_media_url(details, "test-video-id", "myfile.mp4")

        error_msg = str(exc_info.value)
        assert "test-video-id" in error_msg
        assert "myfile.mp4" in error_msg

    # =========================================================================
    # _parse_video_filename tests
    # =========================================================================

    def test_parse_video_filename_old_format(self):
        """Parse old format: grok-video-{uuid}.mp4."""
        fmt, uuid = ResponseParser._parse_video_filename(
            "grok-video-12345678-1234-1234-1234-123456789abc.mp4"
        )
        assert fmt == "old"
        assert uuid == "12345678-1234-1234-1234-123456789abc"

    def test_parse_video_filename_web_format(self):
        """Parse web format: {uuid}.mp4."""
        fmt, uuid = ResponseParser._parse_video_filename("12345678-1234-1234-1234-123456789abc.mp4")
        assert fmt == "web"
        assert uuid == "12345678-1234-1234-1234-123456789abc"

    def test_parse_video_filename_hd_format(self):
        """Parse HD format: {uuid}_hd.mp4."""
        fmt, uuid = ResponseParser._parse_video_filename(
            "12345678-1234-1234-1234-123456789abc_hd.mp4"
        )
        assert fmt == "web"
        assert uuid == "12345678-1234-1234-1234-123456789abc"

    def test_parse_video_filename_invalid(self):
        """Return None for unrecognized filename."""
        fmt, uuid = ResponseParser._parse_video_filename("random-file.txt")
        assert fmt is None
        assert uuid is None
