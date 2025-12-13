"""Tests for GrokClient class (curl_cffi based)."""

from unittest.mock import MagicMock, patch

import pytest

from grok_web.client import GrokClient
from grok_web.exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from grok_web.models import GrokCookies


class TestGrokClientInit:
    """Tests for GrokClient initialization."""

    def test_init_with_cookies(self, mock_cookies: GrokCookies):
        """Initialize with provided cookies."""
        client = GrokClient(cookies=mock_cookies)
        assert client.cookies == mock_cookies

    def test_init_loads_from_config(self, mock_cookies: GrokCookies):
        """Initialize loads cookies from config file."""
        mock_config = {"cookies": mock_cookies, "headers": {}, "impersonate": "chrome120"}

        with patch("grok_web.client.load_config", return_value=mock_config):
            client = GrokClient()
            assert client.cookies == mock_cookies


class TestGrokClientApiRequest:
    """Tests for GrokClient._api_request method (JSON version)."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create GrokClient with mocked session."""
        client = GrokClient(cookies=mock_cookies)
        client._session = MagicMock()
        return client

    def test_api_request_json_success(self, client: GrokClient):
        """Successful API request returns parsed JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"posts": []}
        client._session.request = MagicMock(return_value=mock_response)

        result = client._api_request("POST", "/rest/media/post/list", {"limit": 10})

        assert result == {"posts": []}

    def test_api_request_json_401_raises_auth_error(self, client: GrokClient):
        """401 response raises GrokAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        client._session.request = MagicMock(return_value=mock_response)

        with pytest.raises(GrokAuthError, match="blocked"):
            client._api_request("POST", "/rest/media/post/list", {})

    def test_api_request_json_403_raises_auth_error(self, client: GrokClient):
        """403 response raises GrokAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        client._session.request = MagicMock(return_value=mock_response)

        with pytest.raises(GrokAuthError, match="blocked"):
            client._api_request("POST", "/rest/media/post/list", {})

    def test_api_request_json_404_raises_not_found(self, client: GrokClient):
        """404 response raises GrokNotFoundError."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        client._session.request = MagicMock(return_value=mock_response)

        with pytest.raises(GrokNotFoundError):
            client._api_request("POST", "/rest/media/post/get", {"id": "invalid"})

    def test_api_request_json_500_raises_api_error(self, client: GrokClient):
        """500 response raises GrokAPIError."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        client._session.request = MagicMock(return_value=mock_response)

        with pytest.raises(GrokAPIError, match="API error: 500"):
            client._api_request("POST", "/rest/media/post/list", {})

    def test_api_request_json_invalid_returns_empty_dict(self, client: GrokClient):
        """Invalid JSON returns empty dict."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Invalid JSON")
        client._session.request = MagicMock(return_value=mock_response)

        result = client._api_request("POST", "/rest/media/post/list", {})

        assert result == {}


class TestGrokClientApiRequestText:
    """Tests for GrokClient._api_request_text method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create GrokClient with mocked session."""
        client = GrokClient(cookies=mock_cookies)
        client._session = MagicMock()
        return client

    def test_api_request_text_success(self, client: GrokClient):
        """Successful API request returns response text."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"posts": []}'
        client._session.request = MagicMock(return_value=mock_response)

        result = client._api_request_text("POST", "/rest/media/post/list", {"limit": 10})

        assert result == '{"posts": []}'

    def test_api_request_text_401_raises_auth_error(self, client: GrokClient):
        """401 response raises GrokAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        client._session.request = MagicMock(return_value=mock_response)

        with pytest.raises(GrokAuthError, match="blocked"):
            client._api_request_text("POST", "/rest/media/post/list", {})

    def test_api_request_text_403_raises_auth_error(self, client: GrokClient):
        """403 response raises GrokAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        client._session.request = MagicMock(return_value=mock_response)

        with pytest.raises(GrokAuthError, match="blocked"):
            client._api_request_text("POST", "/rest/media/post/list", {})

    def test_api_request_text_404_raises_not_found(self, client: GrokClient):
        """404 response raises GrokNotFoundError."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        client._session.request = MagicMock(return_value=mock_response)

        with pytest.raises(GrokNotFoundError):
            client._api_request_text("POST", "/rest/media/post/get", {"id": "invalid"})

    def test_api_request_text_500_raises_api_error(self, client: GrokClient):
        """500 response raises GrokAPIError."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        client._session.request = MagicMock(return_value=mock_response)

        with pytest.raises(GrokAPIError, match="API error: 500"):
            client._api_request_text("POST", "/rest/media/post/list", {})

    def test_api_request_text_exception_raises_api_error(self, client: GrokClient):
        """Request exception raises GrokAPIError."""
        client._session.request = MagicMock(side_effect=Exception("Connection failed"))

        with pytest.raises(GrokAPIError, match="Request failed"):
            client._api_request_text("POST", "/rest/media/post/list", {})


class TestGrokClientAssetRequest:
    """Tests for GrokClient._asset_request_head method."""

    @pytest.fixture
    def client(self, mock_cookies: GrokCookies) -> GrokClient:
        """Create GrokClient."""
        return GrokClient(cookies=mock_cookies)

    def test_asset_head_success(self, client: GrokClient):
        """Successful HEAD request returns content length."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-length": "12345"}

        with patch("grok_web.client.requests.head", return_value=mock_response):
            result = client._asset_request_head("https://assets.grok.com/video.mp4")
            assert result == 12345

    def test_asset_head_403_raises_auth_error(self, client: GrokClient):
        """403 response raises GrokAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("grok_web.client.requests.head", return_value=mock_response):
            with pytest.raises(GrokAuthError, match="Asset access denied"):
                client._asset_request_head("https://assets.grok.com/video.mp4")

    def test_asset_head_non_200_raises_api_error(self, client: GrokClient):
        """Non-200 response raises GrokAPIError."""
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("grok_web.client.requests.head", return_value=mock_response):
            with pytest.raises(GrokAPIError, match="Asset request failed"):
                client._asset_request_head("https://assets.grok.com/video.mp4")

    def test_asset_head_no_content_length_raises_error(self, client: GrokClient):
        """Missing Content-Length raises GrokAPIError."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        with patch("grok_web.client.requests.head", return_value=mock_response):
            with pytest.raises(GrokAPIError, match="No Content-Length"):
                client._asset_request_head("https://assets.grok.com/video.mp4")

    def test_asset_head_exception_raises_api_error(self, client: GrokClient):
        """Request exception raises GrokAPIError."""
        with patch("grok_web.client.requests.head", side_effect=Exception("Network error")):
            with pytest.raises(GrokAPIError, match="Asset request failed"):
                client._asset_request_head("https://assets.grok.com/video.mp4")
