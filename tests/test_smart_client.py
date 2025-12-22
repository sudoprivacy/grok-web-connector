"""Tests for SmartGrokClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grok_web.client import SmartGrokClient
from grok_web.exceptions import GrokAuthError
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

        assert client.cookies == mock_cookies
        assert client._http_client is None  # Lazy init
        assert client._browser_client is None  # Lazy init

    def test_init_with_browser_config(self, mock_cookies: GrokCookies):
        """Stores browser config for lazy initialization."""
        client = SmartGrokClient(
            cookies=mock_cookies,
            browser_host="127.0.0.1",
            browser_port=9222,
            browser_headless=True,
        )

        assert client._browser_host == "127.0.0.1"
        assert client._browser_port == 9222
        assert client._browser_headless is True

    def test_init_loads_config_when_no_cookies(self):
        """Loads cookies from config file when not provided."""
        mock_cookies = GrokCookies(
            sso="test-sso",
            sso_rw="test-sso-rw",
            x_userid="test-userid",
            cf_clearance="test-cf",
        )

        with patch("grok_web.client.load_config") as mock_load:
            mock_load.return_value = {"cookies": mock_cookies}

            client = SmartGrokClient(config_path="/custom/path.json")

            assert client.cookies == mock_cookies
            mock_load.assert_called_once_with("/custom/path.json")


class TestSmartGrokClientContextManager:
    """Tests for SmartGrokClient async context manager."""

    @pytest.mark.asyncio
    async def test_aenter_initializes_http_client(self, mock_cookies: GrokCookies):
        """__aenter__ initializes HTTP client."""
        with patch("grok_web.client.AsyncClient") as MockAsyncClient:
            mock_http = AsyncMock()
            MockAsyncClient.return_value = mock_http

            client = SmartGrokClient(cookies=mock_cookies)
            await client.__aenter__()

            MockAsyncClient.assert_called_once_with(mock_cookies)
            mock_http.__aenter__.assert_called_once()
            assert client._http_client == mock_http

    @pytest.mark.asyncio
    async def test_aexit_cleans_up_clients(self, mock_cookies: GrokCookies):
        """__aexit__ cleans up both HTTP and browser clients."""
        mock_http = AsyncMock()
        mock_browser = AsyncMock()

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http
        client._browser_client = mock_browser

        await client.__aexit__(None, None, None)

        mock_http.__aexit__.assert_called_once_with(None, None, None)
        mock_browser.__aexit__.assert_called_once_with(None, None, None)


class TestSmartGrokClientReadAPIs:
    """Tests for SmartGrokClient read APIs (via HTTP)."""

    @pytest.mark.asyncio
    async def test_list_posts_uses_http(self, mock_cookies: GrokCookies):
        """list_posts() uses HTTP client and maps source parameter."""
        mock_http = AsyncMock()
        mock_http.list_posts = AsyncMock(return_value=[])

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        result = await client.list_posts(limit=5, source="favorites")

        # SmartGrokClient maps "favorites" to "MEDIA_POST_SOURCE_LIKED"
        mock_http.list_posts.assert_called_once_with(5, "MEDIA_POST_SOURCE_LIKED", False)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_post_details_uses_http(self, mock_cookies: GrokCookies):
        """get_post_details() uses HTTP client."""
        mock_details = MagicMock(spec=PostDetails)
        mock_http = AsyncMock()
        mock_http.get_post_details = AsyncMock(return_value=mock_details)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        result = await client.get_post_details("test-post-id")

        mock_http.get_post_details.assert_called_once_with("test-post-id")
        assert result == mock_details

    @pytest.mark.asyncio
    async def test_validate_auth_uses_http(self, mock_cookies: GrokCookies):
        """validate_auth() uses HTTP client."""
        mock_http = AsyncMock()
        mock_http.validate_auth = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        result = await client.validate_auth()

        mock_http.validate_auth.assert_called_once()
        assert result is True


class TestSmartGrokClientWriteAPIs:
    """Tests for SmartGrokClient write APIs."""

    @pytest.mark.asyncio
    async def test_favorite_post_uses_http(self, mock_cookies: GrokCookies):
        """favorite_post() uses HTTP client first."""
        mock_http = AsyncMock()
        mock_http.favorite_post = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        result = await client.favorite_post("test-post-id")

        mock_http.favorite_post.assert_called_once_with("test-post-id")
        assert result is True


class TestSmartGrokClientVideoCreation:
    """Tests for SmartGrokClient video creation with fallback."""

    @pytest.mark.asyncio
    async def test_create_video_uses_http_first(self, mock_cookies: GrokCookies):
        """create_video() tries HTTP first."""
        mock_result = VideoGenerationResult(
            video_id="test-video-id",
            parent_post_id="test-post-id",
            moderated=False,
        )
        mock_http = AsyncMock()
        mock_http.get_post_details = AsyncMock(
            return_value=MagicMock(media_url="https://example.com/image.png")
        )
        mock_http.create_video_from_image = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        # Use unified API: empty prompt + source_post_id + preset="fun" to use HTTP path
        result = await client.create_video("", source_post_id="test-post-id", preset="fun")

        mock_http.create_video_from_image.assert_called_once()
        assert result.video_id == "test-video-id"

    @pytest.mark.asyncio
    async def test_create_video_falls_back_to_browser_on_403(self, mock_cookies: GrokCookies):
        """create_video() falls back to browser on 403."""
        mock_result = VideoGenerationResult(
            video_id="browser-video-id",
            parent_post_id="test-post-id",
            moderated=False,
        )

        mock_http = AsyncMock()
        mock_http.get_post_details = AsyncMock(
            return_value=MagicMock(media_url="https://example.com/image.png")
        )
        mock_http.create_video_from_image = AsyncMock(
            side_effect=GrokAuthError("Request blocked (403)")
        )

        mock_browser = AsyncMock()
        mock_browser.create_video = AsyncMock(return_value=mock_result)
        mock_browser.__aenter__ = AsyncMock(return_value=mock_browser)

        with patch("grok_web.client.NodriverClient") as MockNodriver:
            MockNodriver.return_value = mock_browser

            client = SmartGrokClient(
                cookies=mock_cookies,
                browser_host="127.0.0.1",
                browser_port=9222,
            )
            client._http_client = mock_http

            # Use unified API: empty prompt + source_post_id + preset="fun"
            result = await client.create_video("", source_post_id="test-post-id", preset="fun")

            mock_http.create_video_from_image.assert_called_once()
            # SmartGrokClient now delegates to browser.create_video (single source of truth)
            mock_browser.create_video.assert_called_once()
            assert result.video_id == "browser-video-id"

    @pytest.mark.asyncio
    async def test_create_video_raises_when_fallback_disabled(self, mock_cookies: GrokCookies):
        """create_video() raises if 403 and browser fallback disabled."""
        mock_http = AsyncMock()
        mock_http.get_post_details = AsyncMock(
            return_value=MagicMock(media_url="https://example.com/image.png")
        )
        mock_http.create_video_from_image = AsyncMock(
            side_effect=GrokAuthError("Request blocked (403)")
        )

        # Disable browser fallback
        client = SmartGrokClient(cookies=mock_cookies, enable_browser_fallback=False)
        client._http_client = mock_http

        with pytest.raises(GrokAuthError) as exc_info:
            # Use unified API: empty prompt + source_post_id
            await client.create_video("", source_post_id="test-post-id", preset="fun")

        assert "Enable browser fallback" in str(exc_info.value)


class TestSmartGrokClientLazyBrowser:
    """Tests for lazy browser initialization."""

    @pytest.mark.asyncio
    async def test_browser_not_initialized_on_read_operations(self, mock_cookies: GrokCookies):
        """Browser client is not initialized for read operations."""
        mock_http = AsyncMock()
        mock_http.list_posts = AsyncMock(return_value=[])

        client = SmartGrokClient(
            cookies=mock_cookies,
            browser_host="127.0.0.1",
            browser_port=9222,
        )
        client._http_client = mock_http

        await client.list_posts()

        assert client._browser_client is None  # Still None

    @pytest.mark.asyncio
    async def test_browser_initialized_only_when_needed(self, mock_cookies: GrokCookies):
        """Browser client is initialized only when video creation needs fallback."""
        mock_result = VideoGenerationResult(
            video_id="test-id",
            parent_post_id="test-post",
            moderated=False,
        )

        mock_http = AsyncMock()
        mock_http.get_post_details = AsyncMock(
            return_value=MagicMock(image_url="https://example.com/image.png")
        )
        mock_http.create_video_from_image = AsyncMock(side_effect=GrokAuthError("blocked"))

        mock_browser = AsyncMock()
        mock_browser.create_video_via_ui = AsyncMock(return_value=mock_result)
        mock_browser.__aenter__ = AsyncMock(return_value=mock_browser)

        with patch("grok_web.client.NodriverClient") as MockNodriver:
            MockNodriver.return_value = mock_browser

            client = SmartGrokClient(
                cookies=mock_cookies,
                browser_host="127.0.0.1",
                browser_port=9222,
            )
            client._http_client = mock_http

            # First call - browser should be initialized
            await client.create_video("test-post")

            MockNodriver.assert_called_once_with(
                cookies=mock_cookies,
                host="127.0.0.1",
                port=9222,
                headless=False,
            )

            # Second call - browser should be reused
            await client.create_video("test-post-2")

            # Still only one initialization
            assert MockNodriver.call_count == 1


class TestSmartGrokClientUnfavorite:
    """Tests for unfavorite_post."""

    @pytest.mark.asyncio
    async def test_unfavorite_post_uses_http(self, mock_cookies: GrokCookies):
        """unfavorite_post() uses HTTP client first."""
        mock_http = AsyncMock()
        mock_http.unfavorite_post = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        result = await client.unfavorite_post("test-post-id")

        mock_http.unfavorite_post.assert_called_once_with("test-post-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_unfavorite_post_falls_back_to_browser(self, mock_cookies: GrokCookies):
        """unfavorite_post() falls back to browser on 403."""
        mock_http = AsyncMock()
        mock_http.unfavorite_post = AsyncMock(side_effect=GrokAuthError("403"))

        mock_browser = AsyncMock()
        mock_browser.unfavorite_post = AsyncMock(return_value=True)
        mock_browser.__aenter__ = AsyncMock(return_value=mock_browser)

        with patch("grok_web.client.NodriverClient") as MockNodriver:
            MockNodriver.return_value = mock_browser

            client = SmartGrokClient(cookies=mock_cookies)
            client._http_client = mock_http

            result = await client.unfavorite_post("test-post-id")

            mock_browser.unfavorite_post.assert_called_once_with("test-post-id")
            assert result is True


class TestSmartGrokClientBrowserOnlyAPIs:
    """Tests for browser-only APIs (like, dislike, delete, upgrade)."""

    @pytest.mark.asyncio
    async def test_like_post_uses_browser(self, mock_cookies: GrokCookies):
        """like_post() uses browser (no HTTP API exists)."""
        mock_browser = AsyncMock()
        mock_browser.like_post = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.like_post("test-post-id")

        mock_browser.like_post.assert_called_once_with("test-post-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_dislike_post_uses_browser(self, mock_cookies: GrokCookies):
        """dislike_post() uses browser (no HTTP API exists)."""
        mock_browser = AsyncMock()
        mock_browser.dislike_post = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.dislike_post("test-post-id")

        mock_browser.dislike_post.assert_called_once_with("test-post-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_video_uses_browser(self, mock_cookies: GrokCookies):
        """delete_video() uses browser (no HTTP API exists)."""
        mock_browser = AsyncMock()
        mock_browser.delete_video = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.delete_video("test-video-id")

        mock_browser.delete_video.assert_called_once_with("test-video-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_upgrade_video_uses_browser(self, mock_cookies: GrokCookies):
        """upgrade_video() uses browser (no HTTP API exists)."""
        mock_browser = AsyncMock()
        mock_browser.upgrade_video = AsyncMock(return_value=True)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.upgrade_video("test-video-id")

        mock_browser.upgrade_video.assert_called_once_with("test-video-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_upload_image_uses_browser(self, mock_cookies: GrokCookies):
        """upload_image() uses browser to upload local image."""
        mock_browser = AsyncMock()
        mock_browser.upload_image = AsyncMock(return_value="new-post-uuid")

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.upload_image("/path/to/image.jpg", timeout=30)

        mock_browser.upload_image.assert_called_once_with("/path/to/image.jpg", 30)
        assert result == "new-post-uuid"


class TestSmartGrokClientReadFallback:
    """Tests for read API browser fallback on 403."""

    @pytest.mark.asyncio
    async def test_list_posts_falls_back_to_browser(self, mock_cookies: GrokCookies):
        """list_posts() falls back to browser on 403."""
        mock_http = AsyncMock()
        mock_http.list_posts = AsyncMock(side_effect=GrokAuthError("403"))

        mock_browser = AsyncMock()
        mock_browser.list_posts = AsyncMock(return_value=[])
        mock_browser.__aenter__ = AsyncMock(return_value=mock_browser)

        with patch("grok_web.client.NodriverClient") as MockNodriver:
            MockNodriver.return_value = mock_browser

            client = SmartGrokClient(cookies=mock_cookies)
            client._http_client = mock_http

            result = await client.list_posts(limit=5)

            mock_browser.list_posts.assert_called_once()
            assert result == []

    @pytest.mark.asyncio
    async def test_get_post_details_falls_back_to_browser(self, mock_cookies: GrokCookies):
        """get_post_details() falls back to browser on 403."""
        mock_details = MagicMock(spec=PostDetails)
        mock_http = AsyncMock()
        mock_http.get_post_details = AsyncMock(side_effect=GrokAuthError("403"))

        mock_browser = AsyncMock()
        mock_browser.get_post_details = AsyncMock(return_value=mock_details)
        mock_browser.__aenter__ = AsyncMock(return_value=mock_browser)

        with patch("grok_web.client.NodriverClient") as MockNodriver:
            MockNodriver.return_value = mock_browser

            client = SmartGrokClient(cookies=mock_cookies)
            client._http_client = mock_http

            result = await client.get_post_details("test-post-id")

            mock_browser.get_post_details.assert_called_once_with("test-post-id")
            assert result == mock_details

    @pytest.mark.asyncio
    async def test_get_asset_file_size_uses_http(self, mock_cookies: GrokCookies):
        """get_asset_file_size() uses HTTP client first."""
        mock_http = AsyncMock()
        mock_http.get_asset_file_size = AsyncMock(return_value=12345)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        result = await client.get_asset_file_size("https://example.com/asset.mp4")

        mock_http.get_asset_file_size.assert_called_once_with("https://example.com/asset.mp4")
        assert result == 12345

    @pytest.mark.asyncio
    async def test_match_local_video_uses_http(self, mock_cookies: GrokCookies):
        """match_local_video() uses HTTP client first."""
        mock_result = MagicMock(spec=VideoMatchResult)
        mock_http = AsyncMock()
        mock_http.match_local_video = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        result = await client.match_local_video("/path/to/video.mp4")

        mock_http.match_local_video.assert_called_once_with("/path/to/video.mp4")
        assert result == mock_result


class TestSmartGrokClientImageAPIs:
    """Tests for image generation APIs."""

    @pytest.mark.asyncio
    async def test_edit_image_uses_browser(self, mock_cookies: GrokCookies):
        """edit_image() uses browser (no HTTP API exists)."""
        mock_result = MagicMock(spec=ImageEditResult)
        mock_browser = AsyncMock()
        mock_browser.edit_image = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        result = await client.edit_image("test-post-id", "add sunglasses", timeout=30)

        mock_browser.edit_image.assert_called_once_with("test-post-id", "add sunglasses", 30)
        assert result == mock_result

    @pytest.mark.asyncio
    async def test_create_image_uses_browser(self, mock_cookies: GrokCookies):
        """create_image() uses browser (no HTTP API exists)."""
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
        from unittest.mock import MagicMock

        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.body = AsyncMock(return_value=b"video_data")
        mock_context = AsyncMock()
        mock_context.get = AsyncMock(return_value=mock_response)
        mock_http._get_asset_context = AsyncMock(return_value=mock_context)

        mock_child = MagicMock()
        mock_child.id = "video-123"
        mock_child.media_url = "https://example.com/video.mp4"
        mock_child.hd_media_url = "https://example.com/video_hd.mp4"
        mock_details = MagicMock()
        mock_details.children = [mock_child]
        mock_http.get_post_details = AsyncMock(return_value=mock_details)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        with patch("pathlib.Path.write_bytes"):
            with patch("pathlib.Path.mkdir"):
                await client.download_video(
                    "video-123", "/tmp/output.mp4", parent_post_id="parent-456"
                )

        mock_http.get_post_details.assert_called_once_with("parent-456")
        mock_context.get.assert_called_once()
        assert "video_hd.mp4" in mock_context.get.call_args[0][0]

    @pytest.mark.asyncio
    async def test_download_video_falls_back_to_browser_on_403(self, mock_cookies: GrokCookies):
        """download_video() falls back to browser on 403."""
        from pathlib import Path
        from unittest.mock import MagicMock

        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status = 403
        mock_context = AsyncMock()
        mock_context.get = AsyncMock(return_value=mock_response)
        mock_http._get_asset_context = AsyncMock(return_value=mock_context)

        mock_child = MagicMock()
        mock_child.id = "video-123"
        mock_child.media_url = "https://example.com/video.mp4"
        mock_child.hd_media_url = None
        mock_details = MagicMock()
        mock_details.children = [mock_child]
        mock_http.get_post_details = AsyncMock(return_value=mock_details)

        mock_browser = AsyncMock()
        mock_browser.download_video = AsyncMock(return_value=Path("/tmp/output.mp4"))

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http
        client._browser_client = mock_browser

        result = await client.download_video(
            "video-123", "/tmp/output.mp4", parent_post_id="parent-456"
        )

        mock_browser.download_video.assert_called_once()
        assert result == Path("/tmp/output.mp4")

    @pytest.mark.asyncio
    async def test_download_video_raises_not_found(self, mock_cookies: GrokCookies):
        """download_video() raises GrokNotFoundError when video not found."""
        from grok_web.exceptions import GrokNotFoundError

        mock_http = AsyncMock()
        mock_details = MagicMock()
        mock_details.children = []  # No children
        mock_http.get_post_details = AsyncMock(return_value=mock_details)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        with pytest.raises(GrokNotFoundError):
            await client.download_video(
                "nonexistent-video", "/tmp/output.mp4", parent_post_id="parent-456"
            )


class TestSmartGrokClientTxt2Vid:
    """Tests for txt2vid mode of create_video."""

    @pytest.mark.asyncio
    async def test_create_video_txt2vid_uses_browser(self, mock_cookies: GrokCookies):
        """create_video() without source_post_id uses browser directly (txt2vid)."""
        mock_result = VideoGenerationResult(
            video_id="txt2vid-video-id",
            parent_post_id="new-post-id",
            moderated=False,
        )
        mock_browser = AsyncMock()
        mock_browser.create_video = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._browser_client = mock_browser

        # txt2vid: prompt only, no source_post_id
        result = await client.create_video("a cat playing with yarn")

        mock_browser.create_video.assert_called_once()
        call_kwargs = mock_browser.create_video.call_args.kwargs
        assert call_kwargs["prompt"] == "a cat playing with yarn"
        assert call_kwargs["source_post_id"] is None
        assert result.video_id == "txt2vid-video-id"

    @pytest.mark.asyncio
    async def test_create_video_validates_mutual_exclusion(self, mock_cookies: GrokCookies):
        """create_video() raises if both source_post_id and source_image_path provided."""
        client = SmartGrokClient(cookies=mock_cookies)

        with pytest.raises(ValueError) as exc_info:
            await client.create_video(
                "test prompt",
                source_post_id="post-123",
                source_image_path="/path/to/image.jpg",
            )

        assert "Cannot specify both" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_video_upload2vid_uses_browser(self, mock_cookies: GrokCookies):
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

        mock_browser.create_video.assert_called_once_with(
            prompt="test prompt",
            source_image_path="/path/to/image.jpg",
            preset="zoom_in",
            aspect_ratio="portrait",
            timeout=300,
        )
        assert result.video_id == "uploaded-video-123"

    @pytest.mark.asyncio
    async def test_create_video_preset_only_mode(self, mock_cookies: GrokCookies):
        """create_video() with preset only (no prompt) works correctly."""
        mock_http = AsyncMock()
        mock_http.get_post_details.return_value = PostDetails(
            id="post-123",
            mode="img2vid",
            media_url="https://example.com/image.jpg",
            media_type="MEDIA_POST_TYPE_IMAGE",
        )
        mock_http.create_video_from_image.side_effect = GrokAuthError("403")

        mock_browser = AsyncMock()
        mock_browser.create_video.return_value = VideoGenerationResult(
            parent_post_id="parent-123",
            video_id="preset-video-123",
            video_url="https://grok.com/video.mp4",
            moderated=False,
        )

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http
        client._browser_client = mock_browser

        # Call without prompt (preset-only mode)
        result = await client.create_video(
            source_post_id="post-123",
            preset="spicy",
        )

        # Should call browser with empty prompt
        mock_browser.create_video.assert_called_once()
        call_kwargs = mock_browser.create_video.call_args.kwargs
        assert call_kwargs["prompt"] == ""
        assert call_kwargs["source_post_id"] == "post-123"
        assert call_kwargs["preset"] == "spicy"
        assert result.video_id == "preset-video-123"
