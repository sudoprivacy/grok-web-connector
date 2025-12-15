"""Tests for SmartGrokClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grok_web.client import SmartGrokClient
from grok_web.exceptions import GrokAuthError
from grok_web.models import GrokCookies, PostDetails, VideoGenerationResult


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
        """list_posts() uses HTTP client."""
        mock_http = AsyncMock()
        mock_http.list_posts = AsyncMock(return_value=[])

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        result = await client.list_posts(limit=5, source="favorites")

        mock_http.list_posts.assert_called_once_with(5, "favorites", False)
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
            return_value=MagicMock(image_url="https://example.com/image.png")
        )
        mock_http.create_video_from_image = AsyncMock(return_value=mock_result)

        client = SmartGrokClient(cookies=mock_cookies)
        client._http_client = mock_http

        result = await client.create_video("test-post-id", preset="normal")

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
            return_value=MagicMock(image_url="https://example.com/image.png")
        )
        mock_http.create_video_from_image = AsyncMock(
            side_effect=GrokAuthError("Request blocked (403)")
        )

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

            result = await client.create_video("test-post-id", preset="fun")

            mock_http.create_video_from_image.assert_called_once()
            mock_browser.create_video_via_ui.assert_called_once_with("test-post-id", preset="fun")
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
            await client.create_video("test-post-id")

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
