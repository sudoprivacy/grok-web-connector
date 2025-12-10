"""Tests for PlaywrightClient class."""

from unittest.mock import MagicMock, patch

import pytest

from grok_web import PlaywrightClient
from grok_web.exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from grok_web.models import GrokCookies, PostSummary


class TestPlaywrightClientInit:
    """Tests for PlaywrightClient initialization."""

    def test_init_with_cookies(self, mock_cookies: GrokCookies):
        """Initialize with provided cookies."""
        client = PlaywrightClient(cookies=mock_cookies)
        assert client.cookies == mock_cookies
        assert client._playwright is None  # Not started yet

    def test_init_loads_from_config(self, mock_cookies: GrokCookies):
        """Initialize loads cookies from config file."""
        mock_config = {"cookies": mock_cookies, "headers": {}}

        with patch("grok_web.client.load_config", return_value=mock_config):
            client = PlaywrightClient()
            assert client.cookies == mock_cookies

    def test_cookie_string_format(self, mock_cookies: GrokCookies):
        """Cookie string is correctly formatted."""
        client = PlaywrightClient(cookies=mock_cookies)
        # Cookie string should contain all cookies
        assert "sso=mock_sso_token" in client._cookie_str
        assert "cf_clearance=mock_cf_clearance" in client._cookie_str


class TestPlaywrightClientContextManager:
    """Tests for PlaywrightClient context manager."""

    def test_context_manager_starts_playwright(self, mock_cookies: GrokCookies):
        """Context manager starts Playwright."""
        with patch("grok_web.client.sync_playwright") as mock_sync:
            mock_playwright = MagicMock()
            mock_context = MagicMock()
            mock_sync.return_value.start.return_value = mock_playwright
            mock_playwright.request.new_context.return_value = mock_context

            client = PlaywrightClient(cookies=mock_cookies)
            with client as ctx_client:
                assert ctx_client._playwright == mock_playwright
                assert ctx_client._api_context == mock_context

    def test_context_manager_cleanup(self, mock_cookies: GrokCookies):
        """Context manager cleans up resources."""
        with patch("grok_web.client.sync_playwright") as mock_sync:
            mock_playwright = MagicMock()
            mock_context = MagicMock()
            mock_sync.return_value.start.return_value = mock_playwright
            mock_playwright.request.new_context.return_value = mock_context

            client = PlaywrightClient(cookies=mock_cookies)
            with client:
                pass

            # Verify cleanup
            mock_context.dispose.assert_called_once()
            mock_playwright.stop.assert_called_once()


class TestPlaywrightClientAPIRequest:
    """Tests for PlaywrightClient._api_request method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = PlaywrightClient(cookies=mock_cookies)
        client._playwright = MagicMock()
        client._api_context = MagicMock()
        return client

    def test_api_request_post_success(self, mock_client: PlaywrightClient):
        """Successful POST request returns JSON."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json.return_value = {"posts": []}
        mock_client._api_context.post.return_value = mock_response

        result = mock_client._api_request("POST", "/rest/media/post/list", {"limit": 10})
        assert result == {"posts": []}

    def test_api_request_get_success(self, mock_client: PlaywrightClient):
        """Successful GET request returns JSON."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json.return_value = {"data": "value"}
        mock_client._api_context.get.return_value = mock_response

        result = mock_client._api_request("GET", "/some/endpoint")
        assert result == {"data": "value"}

    def test_api_request_unsupported_method(self, mock_client: PlaywrightClient):
        """Unsupported HTTP method raises error."""
        with pytest.raises(GrokAPIError, match="Unsupported HTTP method"):
            mock_client._api_request("DELETE", "/some/endpoint")

    def test_api_request_cloudflare_challenge(self, mock_client: PlaywrightClient):
        """Cloudflare challenge triggers specific error."""
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.text.return_value = "Just a moment..."
        mock_client._api_context.post.return_value = mock_response

        with pytest.raises(GrokAuthError, match="Cloudflare challenge"):
            mock_client._api_request("POST", "/rest/media/post/list", {})

    def test_api_request_401(self, mock_client: PlaywrightClient):
        """401 raises GrokAuthError."""
        mock_response = MagicMock()
        mock_response.status = 401
        mock_response.text.return_value = "Unauthorized"
        mock_client._api_context.post.return_value = mock_response

        with pytest.raises(GrokAuthError):
            mock_client._api_request("POST", "/rest/media/post/list", {})

    def test_api_request_404(self, mock_client: PlaywrightClient):
        """404 raises GrokNotFoundError."""
        mock_response = MagicMock()
        mock_response.status = 404
        mock_client._api_context.post.return_value = mock_response

        with pytest.raises(GrokNotFoundError):
            mock_client._api_request("POST", "/rest/media/post/get", {"id": "invalid"})


class TestPlaywrightClientAssetRequest:
    """Tests for PlaywrightClient._asset_request_head method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = PlaywrightClient(cookies=mock_cookies)
        client._playwright = MagicMock()
        client._api_context = MagicMock()
        client._asset_context = None
        return client

    def test_asset_request_creates_context_lazily(self, mock_client: PlaywrightClient):
        """Asset context is created on first use."""
        mock_asset_context = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"content-length": "1000"}
        mock_asset_context.head.return_value = mock_response

        mock_client._playwright.request.new_context.return_value = mock_asset_context

        size = mock_client._asset_request_head("https://assets.grok.com/video.mp4")

        assert size == 1000
        mock_client._playwright.request.new_context.assert_called_once()

    def test_asset_request_reuses_context(self, mock_client: PlaywrightClient):
        """Asset context is reused on subsequent calls."""
        mock_asset_context = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"content-length": "1000"}
        mock_asset_context.head.return_value = mock_response

        mock_client._asset_context = mock_asset_context

        mock_client._asset_request_head("https://assets.grok.com/video.mp4")
        mock_client._asset_request_head("https://assets.grok.com/video2.mp4")

        # new_context should NOT be called again
        mock_client._playwright.request.new_context.assert_not_called()

    def test_asset_request_403(self, mock_client: PlaywrightClient):
        """403 raises GrokAuthError."""
        mock_asset_context = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 403
        mock_asset_context.head.return_value = mock_response
        mock_client._asset_context = mock_asset_context

        with pytest.raises(GrokAuthError):
            mock_client._asset_request_head("https://assets.grok.com/video.mp4")


class TestPlaywrightClientListPosts:
    """Tests for PlaywrightClient.list_posts inherited method."""

    @pytest.fixture
    def mock_client(self, mock_cookies: GrokCookies):
        """Create client with mocked Playwright."""
        client = PlaywrightClient(cookies=mock_cookies)
        client._playwright = MagicMock()
        client._api_context = MagicMock()
        return client

    def test_list_posts_uses_default_source(self, mock_client: PlaywrightClient):
        """Default source is MEDIA_POST_SOURCE_LIKED."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json.return_value = {"posts": []}
        mock_client._api_context.post.return_value = mock_response

        mock_client.list_posts()

        call_args = mock_client._api_context.post.call_args
        data = call_args[1]["data"]
        assert data["filter"]["source"] == "MEDIA_POST_SOURCE_LIKED"

    def test_list_posts_returns_summaries(
        self, mock_client: PlaywrightClient, sample_list_response: dict
    ):
        """list_posts returns PostSummary objects."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json.return_value = sample_list_response
        mock_client._api_context.post.return_value = mock_response

        posts = mock_client.list_posts()

        assert len(posts) == 2
        assert all(isinstance(p, PostSummary) for p in posts)
