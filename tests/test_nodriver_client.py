"""Tests for NodriverClient class."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grok_web.client import NodriverClient
from grok_web.models import GrokCookies


class TestNodriverClientInit:
    """Tests for NodriverClient initialization."""

    def test_init_with_cookies(self, mock_cookies: GrokCookies):
        """Initialize with explicit cookies."""
        client = NodriverClient(cookies=mock_cookies)
        assert client.cookies == mock_cookies
        assert client._headless is False
        assert client._browser is None
        assert client._tab is None

    def test_init_loads_from_config(self, mock_cookies: GrokCookies):
        """Initialize loads cookies from config file."""
        mock_config = {"cookies": mock_cookies}

        with patch("grok_web.client.load_config", return_value=mock_config):
            client = NodriverClient()
            assert client.cookies == mock_cookies

    def test_init_with_headless(self, mock_cookies: GrokCookies):
        """Initialize with headless mode."""
        client = NodriverClient(cookies=mock_cookies, headless=True)
        assert client._headless is True

    def test_init_with_host_port(self, mock_cookies: GrokCookies):
        """Initialize with custom host and port."""
        client = NodriverClient(
            cookies=mock_cookies,
            host="192.168.1.100",
            port=9223,
        )
        assert client._remote_host == "192.168.1.100"
        assert client._remote_port == 9223
        assert client._auto_launch is True

    def test_init_default_host_port(self, mock_cookies: GrokCookies):
        """Initialize with default host/port values."""
        client = NodriverClient(cookies=mock_cookies)
        assert client._remote_host == "127.0.0.1"
        assert client._remote_port == 9222
        assert client._auto_launch is True

    def test_init_with_only_host(self, mock_cookies: GrokCookies):
        """Initialize with only host uses default port."""
        client = NodriverClient(cookies=mock_cookies, host="192.168.1.100")
        assert client._remote_host == "192.168.1.100"
        assert client._remote_port == 9222

    def test_init_with_only_port(self, mock_cookies: GrokCookies):
        """Initialize with only port uses default host."""
        client = NodriverClient(cookies=mock_cookies, port=9223)
        assert client._remote_host == "127.0.0.1"
        assert client._remote_port == 9223

    def test_init_auto_launch_disabled(self, mock_cookies: GrokCookies):
        """Initialize with auto_launch disabled."""
        client = NodriverClient(cookies=mock_cookies, auto_launch=False)
        assert client._auto_launch is False

    def test_init_custom_config_path(self, mock_cookies: GrokCookies):
        """Initialize with custom config path."""
        mock_config = {"cookies": mock_cookies}

        with patch("grok_web.client.load_config", return_value=mock_config) as mock_load:
            NodriverClient(config_path="/custom/path.json")
            mock_load.assert_called_once_with("/custom/path.json")


class TestNodriverClientBrowserReuse:
    """Tests for browser reuse functionality."""

    @pytest.mark.asyncio
    async def test_aexit_keeps_browser_running(self, mock_cookies: GrokCookies):
        """__aexit__ keeps browser running for reuse."""
        mock_browser = MagicMock()
        mock_browser.stop = MagicMock()

        client = NodriverClient(cookies=mock_cookies)
        client._browser = mock_browser

        await client.__aexit__(None, None, None)

        # Browser should NOT be stopped - kept for reuse
        mock_browser.stop.assert_not_called()


class TestNodriverClientApiRequest:
    """Tests for NodriverClient._api_request method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> NodriverClient:
        """Create NodriverClient with mocked tab."""
        client = NodriverClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_api_request_success(self, client: NodriverClient):
        """Successful API request returns parsed JSON."""
        client._tab.evaluate = AsyncMock(
            return_value='{"status": 200, "body": "{\\"posts\\": []}"}'
        )

        result = await client._api_request("POST", "/rest/media/post/list", {"limit": 10})

        assert result == {"posts": []}

    @pytest.mark.asyncio
    async def test_api_request_401_raises_auth_error(self, client: NodriverClient):
        """401 response raises GrokAuthError."""
        from grok_web.exceptions import GrokAuthError

        client._tab.evaluate = AsyncMock(return_value='{"status": 401, "body": "Unauthorized"}')

        with pytest.raises(GrokAuthError, match="Request blocked"):
            await client._api_request("POST", "/rest/media/post/list", {})

    @pytest.mark.asyncio
    async def test_api_request_403_cloudflare(self, client: NodriverClient):
        """403 with Cloudflare message raises specific error."""
        from grok_web.exceptions import GrokAuthError

        client._tab.evaluate = AsyncMock(return_value='{"status": 403, "body": "Just a moment..."}')

        with pytest.raises(GrokAuthError, match="Cloudflare"):
            await client._api_request("POST", "/rest/media/post/list", {})

    @pytest.mark.asyncio
    async def test_api_request_404_raises_not_found(self, client: NodriverClient):
        """404 response raises GrokNotFoundError."""
        from grok_web.exceptions import GrokNotFoundError

        client._tab.evaluate = AsyncMock(return_value='{"status": 404, "body": "Not found"}')

        with pytest.raises(GrokNotFoundError):
            await client._api_request("POST", "/rest/media/post/get", {"id": "invalid"})

    @pytest.mark.asyncio
    async def test_api_request_500_raises_api_error(self, client: NodriverClient):
        """500 response raises GrokAPIError."""
        from grok_web.exceptions import GrokAPIError

        client._tab.evaluate = AsyncMock(return_value='{"status": 500, "body": "Internal error"}')

        with pytest.raises(GrokAPIError, match="API error: 500"):
            await client._api_request("POST", "/rest/media/post/list", {})


class TestNodriverClientAssetRequest:
    """Tests for NodriverClient._asset_request_head method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> NodriverClient:
        """Create NodriverClient with mocked tab."""
        client = NodriverClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_asset_head_success(self, client: NodriverClient):
        """Successful HEAD request returns content length."""
        client._tab.evaluate = AsyncMock(return_value='{"status": 200, "contentLength": "12345"}')

        result = await client._asset_request_head("https://assets.grok.com/video.mp4")

        assert result == 12345

    @pytest.mark.asyncio
    async def test_asset_head_403_raises(self, client: NodriverClient):
        """403 response raises GrokAuthError."""
        from grok_web.exceptions import GrokAuthError

        client._tab.evaluate = AsyncMock(return_value='{"status": 403, "contentLength": null}')

        with pytest.raises(GrokAuthError, match="Asset access denied"):
            await client._asset_request_head("https://assets.grok.com/video.mp4")

    @pytest.mark.asyncio
    async def test_asset_head_no_content_length_raises(self, client: NodriverClient):
        """Missing Content-Length raises GrokAPIError."""
        from grok_web.exceptions import GrokAPIError

        client._tab.evaluate = AsyncMock(return_value='{"status": 200, "contentLength": null}')

        with pytest.raises(GrokAPIError, match="No Content-Length"):
            await client._asset_request_head("https://assets.grok.com/video.mp4")


class TestNodriverClientApiRequestText:
    """Tests for NodriverClient._api_request_text method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> NodriverClient:
        """Create NodriverClient with mocked tab."""
        client = NodriverClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_api_request_text_success(self, client: NodriverClient):
        """Successful request returns raw text body."""
        client._tab.evaluate = AsyncMock(
            return_value='{"status": 200, "body": "raw response text"}'
        )

        result = await client._api_request_text("POST", "/endpoint", {})

        assert result == "raw response text"

    @pytest.mark.asyncio
    async def test_api_request_text_401_raises(self, client: NodriverClient):
        """401 response raises GrokAuthError."""
        from grok_web.exceptions import GrokAuthError

        client._tab.evaluate = AsyncMock(return_value='{"status": 401, "body": "Unauthorized"}')

        with pytest.raises(GrokAuthError, match="blocked"):
            await client._api_request_text("POST", "/endpoint", {})

    @pytest.mark.asyncio
    async def test_api_request_text_403_cloudflare(self, client: NodriverClient):
        """403 with Cloudflare triggers specific error."""
        from grok_web.exceptions import GrokAuthError

        client._tab.evaluate = AsyncMock(return_value='{"status": 403, "body": "Just a moment..."}')

        with pytest.raises(GrokAuthError, match="Cloudflare"):
            await client._api_request_text("POST", "/endpoint", {})

    @pytest.mark.asyncio
    async def test_api_request_text_404_raises(self, client: NodriverClient):
        """404 response raises GrokNotFoundError."""
        from grok_web.exceptions import GrokNotFoundError

        client._tab.evaluate = AsyncMock(return_value='{"status": 404, "body": "Not found"}')

        with pytest.raises(GrokNotFoundError):
            await client._api_request_text("POST", "/endpoint", {})

    @pytest.mark.asyncio
    async def test_api_request_text_500_raises(self, client: NodriverClient):
        """500 response raises GrokAPIError."""
        from grok_web.exceptions import GrokAPIError

        client._tab.evaluate = AsyncMock(return_value='{"status": 500, "body": "Internal error"}')

        with pytest.raises(GrokAPIError, match="API error: 500"):
            await client._api_request_text("POST", "/endpoint", {})


class TestNodriverClientCreateVideo:
    """Tests for NodriverClient video creation methods."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> NodriverClient:
        """Create NodriverClient with mocked tab."""
        client = NodriverClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_create_video_from_image_uses_page_statsig(self, client: NodriverClient):
        """create_video_from_image tries to get statsig_id from page first."""
        # Mock _get_statsig_id_from_page to return a value
        client._get_statsig_id_from_page = AsyncMock(return_value="page-statsig-id")

        # Mock _api_request_text for the API call
        ndjson_response = '{"result":{"response":{"streamingVideoGenerationResponse":{"videoId":"vid-123","moderated":false}}}}'
        client._api_request_text = AsyncMock(return_value=ndjson_response)

        result = await client.create_video_from_image(
            image_url="https://example.com/image.png",
            parent_post_id="parent-123",
        )

        # Should use the page statsig_id
        assert result.statsig_id == "page-statsig-id"
        client._get_statsig_id_from_page.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_video_from_image_generates_statsig_when_none(
        self, client: NodriverClient
    ):
        """create_video_from_image generates statsig_id when not found on page."""
        # Mock _get_statsig_id_from_page to return None
        client._get_statsig_id_from_page = AsyncMock(return_value=None)

        # Mock _api_request_text for the API call
        ndjson_response = '{"result":{"response":{"streamingVideoGenerationResponse":{"videoId":"vid-123","moderated":false}}}}'
        client._api_request_text = AsyncMock(return_value=ndjson_response)

        result = await client.create_video_from_image(
            image_url="https://example.com/image.png",
            parent_post_id="parent-123",
        )

        # Should have generated a statsig_id
        assert result.statsig_id is not None
        assert len(result.statsig_id) > 50  # Generated IDs are ~94 chars

    @pytest.mark.asyncio
    async def test_create_video_from_image_uses_explicit_statsig(self, client: NodriverClient):
        """create_video_from_image uses explicitly provided statsig_id."""
        client._get_statsig_id_from_page = AsyncMock(return_value="page-id")

        ndjson_response = '{"result":{"response":{"streamingVideoGenerationResponse":{"videoId":"vid-123","moderated":false}}}}'
        client._api_request_text = AsyncMock(return_value=ndjson_response)

        result = await client.create_video_from_image(
            image_url="https://example.com/image.png",
            parent_post_id="parent-123",
            statsig_id="explicit-statsig-id",
        )

        # Should use the explicit statsig_id
        assert result.statsig_id == "explicit-statsig-id"
        # Should NOT call _get_statsig_id_from_page
        client._get_statsig_id_from_page.assert_not_called()


class TestNodriverClientGetStatsigId:
    """Tests for NodriverClient._get_statsig_id_from_page method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> NodriverClient:
        """Create NodriverClient with mocked tab."""
        client = NodriverClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_get_statsig_from_localstorage(self, client: NodriverClient):
        """Get statsig_id from localStorage."""
        client._tab.evaluate = AsyncMock(return_value="found-statsig-id")

        result = await client._get_statsig_id_from_page()

        assert result == "found-statsig-id"

    @pytest.mark.asyncio
    async def test_get_statsig_returns_none_when_not_found(self, client: NodriverClient):
        """Return None when statsig_id not found."""
        client._tab.evaluate = AsyncMock(return_value=None)

        result = await client._get_statsig_id_from_page()

        assert result is None

    @pytest.mark.asyncio
    async def test_get_statsig_handles_exception(self, client: NodriverClient):
        """Return None when exception occurs."""
        client._tab.evaluate = AsyncMock(side_effect=Exception("JS error"))

        result = await client._get_statsig_id_from_page()

        assert result is None


class TestNodriverClientUIMenuOperations:
    """Tests for NodriverClient UI menu operations."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> NodriverClient:
        """Create NodriverClient with mocked tab."""
        client = NodriverClient(cookies=mock_cookies)
        client._tab = AsyncMock()
        client._ui_delay = 0.01  # Fast for tests
        return client

    @pytest.mark.asyncio
    async def test_open_post_menu_success(self, client: NodriverClient):
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
    async def test_open_post_menu_raises_on_404(self, client: NodriverClient):
        """_open_post_menu raises GrokAPIError on 404 page."""
        from grok_web.exceptions import GrokAPIError

        client._tab.get = AsyncMock()
        client._tab.evaluate = AsyncMock(return_value="Page not found - 404")

        with pytest.raises(GrokAPIError, match="404"):
            await client._open_post_menu("nonexistent-post")

    @pytest.mark.asyncio
    async def test_open_post_menu_raises_when_button_not_found(self, client: NodriverClient):
        """_open_post_menu raises GrokAPIError when menu button not found."""
        from grok_web.exceptions import GrokAPIError

        client._tab.get = AsyncMock()
        client._tab.evaluate = AsyncMock(return_value="Normal page")
        client._tab.find = AsyncMock(side_effect=Exception("Not found"))

        with pytest.raises(GrokAPIError, match="menu button"):
            await client._open_post_menu("post-123")

    @pytest.mark.asyncio
    async def test_click_menu_item_success(self, client: NodriverClient):
        """_click_menu_item clicks menu item by text."""
        client._tab.evaluate = AsyncMock(return_value="Save")

        result = await client._click_menu_item("Save", "保存")

        assert result is True

    @pytest.mark.asyncio
    async def test_click_menu_item_raises_when_not_found(self, client: NodriverClient):
        """_click_menu_item raises GrokAPIError when item not found."""
        from grok_web.exceptions import GrokAPIError

        client._tab.evaluate = AsyncMock(return_value=None)

        with pytest.raises(GrokAPIError, match="Could not find menu item"):
            await client._click_menu_item("NonExistent")

    @pytest.mark.asyncio
    async def test_click_confirm_button_success(self, client: NodriverClient):
        """_click_confirm_button clicks confirm button."""
        client._tab.evaluate = AsyncMock(return_value="Delete")

        result = await client._click_confirm_button("Delete", "删除")

        assert result is True

    @pytest.mark.asyncio
    async def test_click_confirm_button_raises_when_not_found(self, client: NodriverClient):
        """_click_confirm_button raises GrokAPIError when button not found."""
        from grok_web.exceptions import GrokAPIError

        client._tab.evaluate = AsyncMock(return_value=None)

        with pytest.raises(GrokAPIError, match="Could not find confirm button"):
            await client._click_confirm_button("NonExistent")

    @pytest.mark.asyncio
    async def test_delete_video_via_ui_success(self, client: NodriverClient):
        """delete_video_via_ui deletes video through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()
        client._click_confirm_button = AsyncMock()

        result = await client.delete_video_via_ui("video-123")

        assert result is True
        client._open_post_menu.assert_called_once_with("video-123")
        client._click_menu_item.assert_called_once()
        client._click_confirm_button.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_video_via_ui_returns_true_on_404(self, client: NodriverClient):
        """delete_video_via_ui returns True when video already deleted (404)."""
        from grok_web.exceptions import GrokAPIError

        client._open_post_menu = AsyncMock(side_effect=GrokAPIError("Post not found (404)"))

        result = await client.delete_video_via_ui("deleted-video")

        assert result is True

    @pytest.mark.asyncio
    async def test_save_post_via_ui_success(self, client: NodriverClient):
        """save_post_via_ui saves post through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()

        result = await client.save_post_via_ui("post-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("保存", "Save")

    @pytest.mark.asyncio
    async def test_unsave_post_via_ui_success(self, client: NodriverClient):
        """unsave_post_via_ui unsaves post through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()

        result = await client.unsave_post_via_ui("post-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("取消保存", "Unsave")

    @pytest.mark.asyncio
    async def test_like_post_via_ui_success(self, client: NodriverClient):
        """like_post_via_ui likes post through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()

        result = await client.like_post_via_ui("post-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("赞", "Like")

    @pytest.mark.asyncio
    async def test_dislike_post_via_ui_success(self, client: NodriverClient):
        """dislike_post_via_ui dislikes post through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()

        result = await client.dislike_post_via_ui("post-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("踩", "Dislike")

    @pytest.mark.asyncio
    async def test_upgrade_video_via_ui_success(self, client: NodriverClient):
        """upgrade_video_via_ui upgrades video through menu."""
        client._open_post_menu = AsyncMock()
        client._click_menu_item = AsyncMock()

        result = await client.upgrade_video_via_ui("video-123")

        assert result is True
        client._click_menu_item.assert_called_once_with("升级视频", "Upgrade video")

    @pytest.mark.asyncio
    async def test_get_menu_items_returns_list(self, client: NodriverClient):
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
