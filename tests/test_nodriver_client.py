"""Tests for GrokClient class."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grok_web.client import GrokClient
from grok_web.models import GrokCookies


class TestGrokClientInit:
    """Tests for GrokClient initialization."""

    def test_init_with_cookies(self, mock_cookies: GrokCookies):
        """Initialize with explicit cookies."""
        client = GrokClient(cookies=mock_cookies)
        assert client._provided_cookies == mock_cookies
        assert client._headless is False
        assert client._browser is None
        assert client._tab is None

    def test_init_without_cookies_defers_loading(self):
        """Initialize without cookies defers loading to __aenter__."""
        client = GrokClient()
        assert client._provided_cookies is None
        assert client.cookies is None

    def test_init_with_headless(self, mock_cookies: GrokCookies):
        """Initialize with headless mode."""
        client = GrokClient(cookies=mock_cookies, headless=True)
        assert client._headless is True

    def test_init_with_host_port(self, mock_cookies: GrokCookies):
        """Initialize with custom host and port."""
        client = GrokClient(
            cookies=mock_cookies,
            host="192.168.1.100",
            port=9223,
        )
        assert client._remote_host == "192.168.1.100"
        assert client._remote_port == 9223
        assert client._auto_launch is True

    def test_init_default_host_port(self, mock_cookies: GrokCookies):
        """Initialize with default host/port values."""
        client = GrokClient(cookies=mock_cookies)
        assert client._remote_host == "127.0.0.1"
        assert client._remote_port is None  # Auto-assigned by ai-dev-browser
        assert client._auto_launch is True

    def test_init_with_only_host(self, mock_cookies: GrokCookies):
        """Initialize with only host uses default port."""
        client = GrokClient(cookies=mock_cookies, host="192.168.1.100")
        assert client._remote_host == "192.168.1.100"
        assert client._remote_port is None  # Auto-assigned by ai-dev-browser

    def test_init_with_only_port(self, mock_cookies: GrokCookies):
        """Initialize with only port uses default host."""
        client = GrokClient(cookies=mock_cookies, port=9223)
        assert client._remote_host == "127.0.0.1"
        assert client._remote_port == 9223

    def test_init_auto_launch_disabled(self, mock_cookies: GrokCookies):
        """Initialize with auto_launch disabled."""
        client = GrokClient(cookies=mock_cookies, auto_launch=False)
        assert client._auto_launch is False

    def test_init_custom_config_path(self, mock_cookies: GrokCookies):
        """Initialize with custom config path stores it."""
        from pathlib import Path

        client = GrokClient(cookies=mock_cookies, config_path="/custom/path.json")
        assert client._config_path == Path("/custom/path.json")


class TestGrokClientBrowserReuse:
    """Tests for browser reuse functionality."""

    @pytest.mark.asyncio
    async def test_aexit_keeps_browser_running(self, mock_cookies: GrokCookies):
        """__aexit__ keeps browser running for reuse."""
        mock_browser = MagicMock()
        mock_browser.stop = MagicMock()

        client = GrokClient(cookies=mock_cookies)
        client._browser = mock_browser

        await client.__aexit__(None, None, None)

        # Browser should NOT be stopped - kept for reuse
        mock_browser.stop.assert_not_called()


class TestGrokClientApiRequest:
    """Tests for GrokClient._api_request method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create GrokClient with mocked tab."""
        client = GrokClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_api_request_success(self, client: GrokClient):
        """Successful API request returns parsed JSON."""
        client._tab.evaluate = AsyncMock(
            return_value='{"status": 200, "body": "{\\"posts\\": []}"}'
        )

        result = await client._api_request("POST", "/rest/media/post/list", {"limit": 10})

        assert result == {"posts": []}

    @pytest.mark.asyncio
    async def test_api_request_401_raises_auth_error(self, client: GrokClient):
        """401 response raises GrokAuthError."""
        from grok_web.exceptions import GrokAuthError

        client._tab.evaluate = AsyncMock(return_value='{"status": 401, "body": "Unauthorized"}')

        with pytest.raises(GrokAuthError, match="Request blocked"):
            await client._api_request("POST", "/rest/media/post/list", {})

    @pytest.mark.asyncio
    async def test_api_request_403_cloudflare(self, client: GrokClient):
        """403 with Cloudflare message raises specific error."""
        from grok_web.exceptions import GrokAuthError

        client._tab.evaluate = AsyncMock(return_value='{"status": 403, "body": "Just a moment..."}')

        with pytest.raises(GrokAuthError, match="Cloudflare"):
            await client._api_request("POST", "/rest/media/post/list", {})

    @pytest.mark.asyncio
    async def test_api_request_404_raises_not_found(self, client: GrokClient):
        """404 response raises GrokNotFoundError."""
        from grok_web.exceptions import GrokNotFoundError

        client._tab.evaluate = AsyncMock(return_value='{"status": 404, "body": "Not found"}')

        with pytest.raises(GrokNotFoundError):
            await client._api_request("POST", "/rest/media/post/get", {"id": "invalid"})

    @pytest.mark.asyncio
    async def test_api_request_500_raises_api_error(self, client: GrokClient):
        """500 response raises GrokAPIError."""
        from grok_web.exceptions import GrokAPIError

        client._tab.evaluate = AsyncMock(return_value='{"status": 500, "body": "Internal error"}')

        with pytest.raises(GrokAPIError, match="API error: 500"):
            await client._api_request("POST", "/rest/media/post/list", {})


class TestGrokClientAssetRequest:
    """Tests for GrokClient._asset_request_head method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create GrokClient with mocked tab."""
        client = GrokClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_asset_head_success(self, client: GrokClient):
        """Successful HEAD request returns content length."""
        # Mock CDP responses
        mock_frame_tree = MagicMock()
        mock_frame_tree.frame.id_ = "test-frame-id"

        mock_response = MagicMock()
        mock_response.success = True
        mock_response.http_status_code = 200
        mock_response.headers = {"content-length": "12345"}
        mock_response.net_error_name = None

        client._tab.send = AsyncMock(side_effect=[mock_frame_tree, mock_response])

        result = await client._asset_request_head("https://assets.grok.com/video.mp4")

        assert result == 12345

    @pytest.mark.asyncio
    async def test_asset_head_403_raises(self, client: GrokClient):
        """403 response raises GrokAuthError."""
        from grok_web.exceptions import GrokAuthError

        # Mock CDP responses
        mock_frame_tree = MagicMock()
        mock_frame_tree.frame.id_ = "test-frame-id"

        mock_response = MagicMock()
        mock_response.success = True
        mock_response.http_status_code = 403
        mock_response.headers = {}
        mock_response.net_error_name = None

        client._tab.send = AsyncMock(side_effect=[mock_frame_tree, mock_response])

        with pytest.raises(GrokAuthError, match="Asset access denied"):
            await client._asset_request_head("https://assets.grok.com/video.mp4")

    @pytest.mark.asyncio
    async def test_asset_head_no_content_length_raises(self, client: GrokClient):
        """Missing Content-Length raises GrokAPIError."""
        from grok_web.exceptions import GrokAPIError

        # Mock CDP responses
        mock_frame_tree = MagicMock()
        mock_frame_tree.frame.id_ = "test-frame-id"

        mock_response = MagicMock()
        mock_response.success = True
        mock_response.http_status_code = 200
        mock_response.headers = {}  # No Content-Length header
        mock_response.net_error_name = None

        client._tab.send = AsyncMock(side_effect=[mock_frame_tree, mock_response])

        with pytest.raises(GrokAPIError, match="No Content-Length"):
            await client._asset_request_head("https://assets.grok.com/video.mp4")


class TestGrokClientUIMenuOperations:
    """Tests for GrokClient UI menu operations."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create GrokClient with mocked tab."""
        client = GrokClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        client._ui_delay = 0.01  # Fast for tests
        return client

    @pytest.mark.asyncio
    async def test_open_post_menu_success(self, client: GrokClient):
        """_open_post_menu navigates and clicks menu button."""
        mock_btn = AsyncMock()
        client._tab.get = AsyncMock()
        client._tab.evaluate = AsyncMock(return_value="Normal page content")
        client._tab.find = AsyncMock(return_value=mock_btn)

        result = await client._open_post_menu("post-123")

        assert result is True
        client._tab.get.assert_called_once()
        mock_btn.scroll_into_view.assert_called_once()
        mock_btn.mouse_click.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_post_menu_raises_on_404(self, client: GrokClient):
        """_open_post_menu raises GrokAPIError on 404 page."""
        from grok_web.exceptions import GrokAPIError

        client._tab.get = AsyncMock()
        client._tab.evaluate = AsyncMock(return_value="Page not found - 404")

        with pytest.raises(GrokAPIError, match="404"):
            await client._open_post_menu("nonexistent-post")

    @pytest.mark.asyncio
    async def test_open_post_menu_raises_when_button_not_found(self, client: GrokClient):
        """_open_post_menu raises GrokAPIError when menu button not found."""
        from grok_web.exceptions import GrokAPIError

        client._tab.get = AsyncMock()
        client._tab.evaluate = AsyncMock(return_value="Normal page")
        client._tab.find = AsyncMock(side_effect=Exception("Not found"))

        with pytest.raises(GrokAPIError, match="menu button"):
            await client._open_post_menu("post-123")

    @pytest.mark.asyncio
    async def test_click_menu_item_success(self, client: GrokClient):
        """_click_menu_item clicks menu item by text."""
        # Mock menu item element with .text property
        mock_item = MagicMock()
        mock_item.text = "Save"
        mock_item.scroll_into_view = AsyncMock()
        mock_item.mouse_click = AsyncMock()

        client._tab.find_all = AsyncMock(return_value=[mock_item])

        result = await client._click_menu_item("Save", "保存")

        assert result is True
        mock_item.mouse_click.assert_called_once()

    @pytest.mark.asyncio
    async def test_click_menu_item_raises_when_not_found(self, client: GrokClient):
        """_click_menu_item raises GrokAPIError when item not found."""
        from grok_web.exceptions import GrokAPIError

        # Return empty list (no menu items found)
        client._tab.find_all = AsyncMock(return_value=[])

        with pytest.raises(GrokAPIError, match="Could not find menu item"):
            await client._click_menu_item("NonExistent")

    @pytest.mark.asyncio
    async def test_click_confirm_button_success(self, client: GrokClient):
        """_click_confirm_button clicks confirm button."""
        client._tab.evaluate = AsyncMock(return_value="Delete")

        result = await client._click_confirm_button("Delete", "删除")

        assert result is True

    @pytest.mark.asyncio
    async def test_click_confirm_button_raises_when_not_found(self, client: GrokClient):
        """_click_confirm_button raises GrokAPIError when button not found."""
        from grok_web.exceptions import GrokAPIError

        client._tab.evaluate = AsyncMock(return_value=None)

        with pytest.raises(GrokAPIError, match="Could not find confirm button"):
            await client._click_confirm_button("NonExistent")

    @pytest.mark.asyncio
    async def test_delete_video_success(self, client: GrokClient):
        """delete_video deletes video through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()
        client._click_confirm_button = AsyncMock()

        result = await client.delete_video("video-123")

        assert result is True
        client._open_post_menu.assert_called_once_with("video-123")
        client._click_menu_item.assert_called_once()
        client._click_confirm_button.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_video_returns_true_on_404(self, client: GrokClient):
        """delete_video returns True when video already deleted (404)."""
        from grok_web.exceptions import GrokAPIError

        client._open_post_menu = AsyncMock(side_effect=GrokAPIError("Post not found (404)"))

        result = await client.delete_video("deleted-video")

        assert result is True

    @pytest.mark.asyncio
    async def test_favorite_post_browser_success(self, client: GrokClient):
        """_favorite_post_browser saves post through menu."""
        client._open_post_menu = AsyncMock()
        client._is_post_favorited = AsyncMock(return_value=False)  # Not favorited
        client._click_menu_item = AsyncMock()

        result = await client._favorite_post_browser("post-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("保存", "Save")

    @pytest.mark.asyncio
    async def test_favorite_post_browser_already_favorited(self, client: GrokClient):
        """_favorite_post_browser skips click if already favorited."""
        client._open_post_menu = AsyncMock()
        client._is_post_favorited = AsyncMock(return_value=True)  # Already favorited
        client._tab.evaluate = AsyncMock()  # For closing menu

        result = await client._favorite_post_browser("post-123")

        assert result is True
        # Should not call _click_menu_item since already favorited

    @pytest.mark.asyncio
    async def test_unfavorite_post_browser_success(self, client: GrokClient):
        """_unfavorite_post_browser unsaves post through menu."""
        client._open_post_menu = AsyncMock()
        client._is_post_favorited = AsyncMock(return_value=True)  # Is favorited
        client._click_menu_item = AsyncMock()

        result = await client._unfavorite_post_browser("post-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("取消保存", "Unsave")

    @pytest.mark.asyncio
    async def test_unfavorite_post_browser_already_not_favorited(self, client: GrokClient):
        """_unfavorite_post_browser skips click if not favorited."""
        client._open_post_menu = AsyncMock()
        client._is_post_favorited = AsyncMock(return_value=False)  # Not favorited
        client._tab.evaluate = AsyncMock()  # For closing menu

        result = await client._unfavorite_post_browser("post-123")

        assert result is True
        # Should not call _click_menu_item since not favorited

    @pytest.mark.asyncio
    async def test_like_post_success(self, client: GrokClient):
        """like_post gives thumbs-up through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()

        result = await client.like_post("post-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("赞", "Like")

    @pytest.mark.asyncio
    async def test_dislike_post_success(self, client: GrokClient):
        """dislike_post gives thumbs-down through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()

        result = await client.dislike_post("post-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("踩", "Dislike")

    @pytest.mark.asyncio
    async def test_upgrade_video_success(self, client: GrokClient):
        """upgrade_video upgrades video through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()

        result = await client.upgrade_video("video-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("升级视频", "Upgrade video")

    @pytest.mark.asyncio
    async def test_get_menu_items_returns_list(self, client: GrokClient):
        """get_menu_items returns list of menu item texts."""
        client._open_post_menu = AsyncMock()
        client._tab.evaluate = AsyncMock(
            side_effect=[
                '["Save", "Delete", "Like"]',  # JSON from querySelectorAll
                None,  # body.click() to close menu
            ]
        )

        result = await client.get_menu_items("post-123")

        assert result == ["Save", "Delete", "Like"]


class TestGrokClientDownloadVideoByUrl:
    """Tests for _download_video_by_url browser-based download."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create GrokClient with mocked tab."""
        client = GrokClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_download_video_success(self, client: GrokClient):
        """_download_video_by_url successfully downloads via browser fetch."""
        import base64
        import json
        from pathlib import Path

        video_data = b"fake_video_content"
        encoded = base64.b64encode(video_data).decode()
        client._tab.evaluate = AsyncMock(
            side_effect=[
                "https://grok.com/imagine",  # current URL check
                json.dumps({"status": 200, "data": encoded}),  # fetch result
            ]
        )

        with patch("pathlib.Path.write_bytes") as mock_write:
            with patch("pathlib.Path.mkdir"):
                result = await client._download_video_by_url(
                    "https://example.com/video.mp4", Path("/tmp/output.mp4")
                )

        mock_write.assert_called_once_with(video_data)
        assert result == Path("/tmp/output.mp4")

    @pytest.mark.asyncio
    async def test_download_video_navigates_if_not_on_grok(self, client: GrokClient):
        """_download_video_by_url navigates to grok.com if not already there."""
        import base64
        import json
        from pathlib import Path

        video_data = b"fake_video_content"
        encoded = base64.b64encode(video_data).decode()
        client._tab.evaluate = AsyncMock(
            side_effect=[
                "https://other-site.com",  # not on grok.com
                json.dumps({"status": 200, "data": encoded}),  # fetch result
            ]
        )
        client._tab.get = AsyncMock()

        with patch("pathlib.Path.write_bytes"):
            with patch("pathlib.Path.mkdir"):
                with patch("asyncio.sleep", return_value=None):
                    await client._download_video_by_url(
                        "https://example.com/video.mp4", Path("/tmp/output.mp4")
                    )

        client._tab.get.assert_called_once_with("https://grok.com/imagine")

    @pytest.mark.asyncio
    async def test_download_video_raises_on_http_error(self, client: GrokClient):
        """_download_video_by_url raises GrokAPIError on HTTP error."""
        import json
        from pathlib import Path

        from grok_web.exceptions import GrokAPIError

        client._tab.evaluate = AsyncMock(
            side_effect=[
                "https://grok.com/imagine",  # current URL check
                json.dumps({"status": 403, "error": "HTTP 403 Forbidden"}),  # fetch error
            ]
        )

        with pytest.raises(GrokAPIError) as exc_info:
            await client._download_video_by_url(
                "https://example.com/video.mp4", Path("/tmp/output.mp4")
            )

        assert "403" in str(exc_info.value)


class TestGrokClientStableId:
    """Tests for stable_id (A/B testing) management."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create GrokClient with mocked tab."""
        client = GrokClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_set_stable_id_success_with_reload(self, client: GrokClient):
        """set_stable_id injects and verifies stable_id after reload."""
        client._tab.evaluate = AsyncMock(
            side_effect=[
                "test-stable-id",  # inject result
                "https://grok.com/imagine",  # current URL for reload
                "test-stable-id",  # get_stable_id verification
            ]
        )
        client._tab.get = AsyncMock()

        with patch("asyncio.sleep", return_value=None):
            result = await client.set_stable_id("test-stable-id", reload_page=True)

        assert result is True
        client._tab.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_stable_id_without_reload(self, client: GrokClient):
        """set_stable_id without reload returns True immediately."""
        client._tab.evaluate = AsyncMock(return_value="test-stable-id")

        result = await client.set_stable_id("test-stable-id", reload_page=False)

        assert result is True
        # evaluate should only be called once for inject
        assert client._tab.evaluate.call_count == 1

    @pytest.mark.asyncio
    async def test_set_stable_id_returns_false_on_mismatch(self, client: GrokClient):
        """set_stable_id returns False if verification fails."""
        client._tab.evaluate = AsyncMock(
            side_effect=[
                "test-stable-id",  # inject result
                "https://grok.com/imagine",  # current URL for reload
                "different-id",  # get_stable_id returns different value
            ]
        )
        client._tab.get = AsyncMock()

        with patch("asyncio.sleep", return_value=None):
            result = await client.set_stable_id("test-stable-id", reload_page=True)

        assert result is False

    @pytest.mark.asyncio
    async def test_set_stable_id_returns_false_on_exception(self, client: GrokClient):
        """set_stable_id returns False on exception."""
        client._tab.evaluate = AsyncMock(side_effect=Exception("Failed"))

        result = await client.set_stable_id("test-stable-id", reload_page=False)

        assert result is False
