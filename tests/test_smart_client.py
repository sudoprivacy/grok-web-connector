"""Tests for SmartGrokClient."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grok_web.client import SmartGrokClient
from grok_web.exceptions import GrokNotFoundError
from grok_web.models import (
    GrokCookies,
    ImageEditResult,
    ImageGenerationResult,
    PostDetails,
    VideoGenerationResult,
    VideoMatchResult,
)


class TestSmartGrokClientInit:
    """Tests for SmartGrokClient initialization."""

    def test_init_with_cookies(self, mock_cookies: GrokCookies):
        """Can initialize with cookies directly."""
        client = SmartGrokClient(cookies=mock_cookies)

        # Cookies are set during __aenter__, stored in _provided_cookies until then
        assert client._provided_cookies == mock_cookies
        assert client._browser_client is None  # Not yet initialized

    def test_init_with_browser_config(self, mock_cookies: GrokCookies):
        """Stores browser config for lazy initialization."""
        client = SmartGrokClient(
            cookies=mock_cookies,
            browser_host="127.0.0.1",
            browser_port=9350,
            browser_headless=True,
        )

        assert client._browser_host == "127.0.0.1"
        assert client._browser_port == 9350
        assert client._browser_headless is True

    @pytest.mark.asyncio
    async def test_init_loads_config_when_no_cookies(self):
        """Loads cookies from config file when not provided (during __aenter__)."""
        mock_cookies = GrokCookies(
            sso="test-sso",
            sso_rw="test-sso-rw",
            x_userid="test-userid",
            cf_clearance="test-cf",
        )

        mock_browser = AsyncMock()
        mock_browser.__aenter__ = AsyncMock(return_value=mock_browser)

        with (
            patch("grok_web.client.load_config") as mock_load,
            patch("grok_web.client.NodriverClient") as MockNodriver,
        ):
            mock_load.return_value = {"cookies": mock_cookies}
            MockNodriver.return_value = mock_browser

            client = SmartGrokClient(config_path="/custom/path.json")
            await client.__aenter__()

            assert client.cookies == mock_cookies
            mock_load.assert_called_once()


class TestSmartGrokClientContextManager:
    """Tests for SmartGrokClient async context manager."""

    @pytest.mark.asyncio
    async def test_aenter_initializes_browser_client(self, mock_cookies: GrokCookies):
        """__aenter__ initializes NodriverClient."""
        mock_browser = AsyncMock()
        mock_browser.__aenter__ = AsyncMock(return_value=mock_browser)

        with patch("grok_web.client.NodriverClient") as MockNodriver:
            MockNodriver.return_value = mock_browser

            client = SmartGrokClient(cookies=mock_cookies)
            await client.__aenter__()

            MockNodriver.assert_called_once_with(
                cookies=mock_cookies,
                config_path=client._config_path,
                host=None,
                port=None,
                headless=False,
            )
            mock_browser.__aenter__.assert_called_once()
            assert client._browser_client == mock_browser

    @pytest.mark.asyncio
    async def test_aexit_cleans_up_browser_client(self, mock_cookies: GrokCookies):
        """__aexit__ cleans up browser client."""
        mock_browser = AsyncMock()

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        await client.__aexit__(None, None, None)

        mock_browser.__aexit__.assert_called_once_with(None, None, None)


class TestSmartGrokClientReadAPIs:
    """Tests for SmartGrokClient read APIs (via NodriverClient)."""

    @pytest.mark.asyncio
    async def test_list_posts_delegates_to_browser(self, mock_cookies: GrokCookies):
        """list_posts() delegates to browser client and maps source parameter."""
        mock_browser = AsyncMock()
        mock_browser.list_posts = AsyncMock(return_value=[])

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.list_posts(limit=5, source="favorites")

        # SmartGrokClient maps "favorites" to "MEDIA_POST_SOURCE_LIKED"
        mock_browser.list_posts.assert_called_once_with(5, "MEDIA_POST_SOURCE_LIKED", False)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_post_details_delegates_to_browser(self, mock_cookies: GrokCookies):
        """get_post_details() delegates to browser client."""
        mock_details = MagicMock(spec=PostDetails)
        mock_browser = AsyncMock()
        mock_browser.get_post_details = AsyncMock(return_value=mock_details)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.get_post_details("test-post-id")

        mock_browser.get_post_details.assert_called_once_with("test-post-id")
        assert result == mock_details

    @pytest.mark.asyncio
    async def test_validate_auth_delegates_to_browser(self, mock_cookies: GrokCookies):
        """validate_auth() delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.validate_auth = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.validate_auth()

        mock_browser.validate_auth.assert_called_once()
        assert result is True

    @pytest.mark.asyncio
    async def test_get_asset_file_size_delegates_to_browser(self, mock_cookies: GrokCookies):
        """get_asset_file_size() delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.get_asset_file_size = AsyncMock(return_value=12345)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.get_asset_file_size("https://example.com/asset.mp4")

        mock_browser.get_asset_file_size.assert_called_once_with("https://example.com/asset.mp4")
        assert result == 12345

    @pytest.mark.asyncio
    async def test_match_local_video_delegates_to_browser(self, mock_cookies: GrokCookies):
        """match_local_video() delegates to browser client."""
        mock_result = MagicMock(spec=VideoMatchResult)
        mock_browser = AsyncMock()
        mock_browser.match_local_video = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.match_local_video("/path/to/video.mp4")

        mock_browser.match_local_video.assert_called_once_with("/path/to/video.mp4")
        assert result == mock_result


class TestSmartGrokClientWriteAPIs:
    """Tests for SmartGrokClient write APIs."""

    @pytest.mark.asyncio
    async def test_favorite_post_delegates_to_browser(self, mock_cookies: GrokCookies):
        """favorite_post() delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.favorite_post = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.favorite_post("test-post-id")

        mock_browser.favorite_post.assert_called_once_with("test-post-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_unfavorite_post_delegates_to_browser(self, mock_cookies: GrokCookies):
        """unfavorite_post() delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.unfavorite_post = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.unfavorite_post("test-post-id")

        mock_browser.unfavorite_post.assert_called_once_with("test-post-id")
        assert result is True


class TestSmartGrokClientSocialAPIs:
    """Tests for social APIs (like, dislike)."""

    @pytest.mark.asyncio
    async def test_like_post_delegates_to_browser(self, mock_cookies: GrokCookies):
        """like_post() delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.like_post = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.like_post("test-post-id")

        mock_browser.like_post.assert_called_once_with("test-post-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_dislike_post_delegates_to_browser(self, mock_cookies: GrokCookies):
        """dislike_post() delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.dislike_post = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.dislike_post("test-post-id")

        mock_browser.dislike_post.assert_called_once_with("test-post-id")
        assert result is True


class TestSmartGrokClientVideoAPIs:
    """Tests for video management APIs (delete, upgrade)."""

    @pytest.mark.asyncio
    async def test_delete_video_delegates_to_browser(self, mock_cookies: GrokCookies):
        """delete_video() delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.delete_video = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.delete_video("test-video-id")

        mock_browser.delete_video.assert_called_once_with("test-video-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_upgrade_video_delegates_to_browser(self, mock_cookies: GrokCookies):
        """upgrade_video() delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.upgrade_video = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.upgrade_video("test-video-id")

        mock_browser.upgrade_video.assert_called_once_with("test-video-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_upload_image_delegates_to_browser(self, mock_cookies: GrokCookies):
        """upload_image() delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.upload_image = AsyncMock(return_value="new-post-uuid")

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.upload_image("/path/to/image.jpg", timeout=30)

        mock_browser.upload_image.assert_called_once_with("/path/to/image.jpg", 30)
        assert result == "new-post-uuid"


class TestSmartGrokClientVideoCreation:
    """Tests for SmartGrokClient video creation."""

    @pytest.mark.asyncio
    async def test_create_video_img2vid(self, mock_cookies: GrokCookies):
        """create_video() with source_post_id delegates to browser client."""
        mock_result = VideoGenerationResult(
            video_id="test-video-id",
            parent_post_id="test-post-id",
            moderated=False,
        )
        mock_browser = AsyncMock()
        mock_browser.create_video = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.create_video("", source_post_id="test-post-id", preset="fun")

        mock_browser.create_video.assert_called_once()
        assert result.video_id == "test-video-id"

    @pytest.mark.asyncio
    async def test_create_video_txt2vid(self, mock_cookies: GrokCookies):
        """create_video() without source_post_id uses txt2vid mode."""
        mock_result = VideoGenerationResult(
            video_id="txt2vid-video-id",
            parent_post_id="new-post-id",
            moderated=False,
        )
        mock_browser = AsyncMock()
        mock_browser.create_video = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.create_video("a cat playing with yarn")

        mock_browser.create_video.assert_called_once()
        call_kwargs = mock_browser.create_video.call_args.kwargs
        assert call_kwargs["prompt"] == "a cat playing with yarn"
        assert call_kwargs["source_post_id"] is None
        assert result.video_id == "txt2vid-video-id"

    @pytest.mark.asyncio
    async def test_create_video_validates_mutual_exclusion(self, mock_cookies: GrokCookies):
        """create_video() raises if both source_post_id and source_image_path provided."""
        mock_browser = AsyncMock()
        mock_browser.create_video = AsyncMock(
            side_effect=ValueError("Cannot specify both source_post_id and source_image_path.")
        )
        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        with pytest.raises(ValueError) as exc_info:
            await client.create_video(
                "test prompt",
                source_post_id="post-123",
                source_image_path="/path/to/image.jpg",
            )

        assert "Cannot specify both" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_video_upload2vid(self, mock_cookies: GrokCookies):
        """create_video() with source_image_path delegates to browser client."""
        mock_browser = AsyncMock()
        mock_browser.create_video.return_value = VideoGenerationResult(
            parent_post_id="parent-123",
            video_id="uploaded-video-123",
            video_url="https://grok.com/video.mp4",
            moderated=False,
        )

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.create_video(
            "test prompt",
            source_image_path="/path/to/image.jpg",
            preset="zoom_in",
        )

        mock_browser.create_video.assert_called_once()
        call_kwargs = mock_browser.create_video.call_args.kwargs
        assert call_kwargs["prompt"] == "test prompt"
        assert call_kwargs["source_image_path"] == "/path/to/image.jpg"
        assert call_kwargs["preset"] == "zoom_in"
        assert result.video_id == "uploaded-video-123"

    @pytest.mark.asyncio
    async def test_create_video_preset_only_mode(self, mock_cookies: GrokCookies):
        """create_video() with preset only (no prompt) works correctly."""
        mock_browser = AsyncMock()
        mock_browser.create_video.return_value = VideoGenerationResult(
            parent_post_id="parent-123",
            video_id="preset-video-123",
            video_url="https://grok.com/video.mp4",
            moderated=False,
        )

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.create_video(
            source_post_id="post-123",
            preset="spicy",
        )

        mock_browser.create_video.assert_called_once()
        call_kwargs = mock_browser.create_video.call_args.kwargs
        assert call_kwargs["prompt"] == ""
        assert call_kwargs["source_post_id"] == "post-123"
        assert call_kwargs["preset"] == "spicy"
        assert result.video_id == "preset-video-123"

    @pytest.mark.asyncio
    async def test_create_video_passes_duration_and_resolution(self, mock_cookies: GrokCookies):
        """create_video() passes duration and resolution to browser client."""
        mock_result = VideoGenerationResult(
            video_id="hd-video-id",
            parent_post_id="test-post-id",
            moderated=False,
        )
        mock_browser = AsyncMock()
        mock_browser.create_video = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.create_video(
            "zoom in",
            source_post_id="test-post-id",
            duration=5,
            resolution="1080p",
        )

        mock_browser.create_video.assert_called_once()
        call_kwargs = mock_browser.create_video.call_args.kwargs
        assert call_kwargs["duration"] == 5
        assert call_kwargs["resolution"] == "1080p"
        assert result.video_id == "hd-video-id"


class TestSmartGrokClientImageAPIs:
    """Tests for image generation APIs."""

    @pytest.mark.asyncio
    async def test_edit_image_delegates_to_browser(self, mock_cookies: GrokCookies):
        """edit_image() delegates to browser client."""
        mock_result = MagicMock(spec=ImageEditResult)
        mock_browser = AsyncMock()
        mock_browser.edit_image = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.edit_image("test-post-id", "add sunglasses", timeout=30)

        mock_browser.edit_image.assert_called_once_with("test-post-id", "add sunglasses", 30)
        assert result == mock_result

    @pytest.mark.asyncio
    async def test_create_image_delegates_to_browser(self, mock_cookies: GrokCookies):
        """create_image() delegates to browser client."""
        mock_result = MagicMock(spec=ImageGenerationResult)
        mock_browser = AsyncMock()
        mock_browser.create_image = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.create_image(
            "a cat wearing sunglasses",
            aspect_ratio="square",
            min_success=2,
            max_scroll=3,
            timeout=60,
        )

        mock_browser.create_image.assert_called_once_with(
            "a cat wearing sunglasses", "square", 2, 3, 60, None
        )
        assert result == mock_result


class TestSmartGrokClientDownloadVideo:
    """Tests for download_video functionality."""

    @pytest.mark.asyncio
    async def test_download_video_with_parent_post_id(self, mock_cookies: GrokCookies):
        """download_video() with parent_post_id does fast lookup."""
        mock_child = MagicMock()
        mock_child.id = "video-123"
        mock_child.media_url = "https://example.com/video.mp4"
        mock_child.hd_media_url = "https://example.com/video_hd.mp4"
        mock_details = MagicMock()
        mock_details.children = [mock_child]

        mock_browser = AsyncMock()
        mock_browser.get_post_details = AsyncMock(return_value=mock_details)
        mock_browser.download_video = AsyncMock(return_value=Path("/tmp/output.mp4"))

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.download_video(
            "video-123", "/tmp/output.mp4", parent_post_id="parent-456"
        )

        mock_browser.get_post_details.assert_called_once_with("parent-456")
        mock_browser.download_video.assert_called_once()
        assert "video_hd.mp4" in mock_browser.download_video.call_args[0][0]
        assert result == Path("/tmp/output.mp4")

    @pytest.mark.asyncio
    async def test_download_video_raises_not_found(self, mock_cookies: GrokCookies):
        """download_video() raises GrokNotFoundError when video not found."""
        mock_details = MagicMock()
        mock_details.children = []  # No children

        mock_browser = AsyncMock()
        mock_browser.get_post_details = AsyncMock(return_value=mock_details)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        with pytest.raises(GrokNotFoundError):
            await client.download_video(
                "nonexistent-video", "/tmp/output.mp4", parent_post_id="parent-456"
            )
