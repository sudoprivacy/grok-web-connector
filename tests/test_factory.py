"""Tests for factory function (get_client)."""

from unittest.mock import patch

from grok_web import get_client
from grok_web.client import SmartGrokClient
from grok_web.models import GrokCookies


class TestGetClient:
    """Tests for get_client() factory function."""

    def test_returns_smart_client(self, mock_cookies: GrokCookies):
        """Returns SmartGrokClient."""
        client = get_client(cookies=mock_cookies)

        assert isinstance(client, SmartGrokClient)

    def test_passes_browser_host_port(self, mock_cookies: GrokCookies):
        """Browser host and port are stored for lazy initialization."""
        client = get_client(
            cookies=mock_cookies,
            browser_host="127.0.0.1",
            browser_port=9222,
        )

        assert isinstance(client, SmartGrokClient)
        assert client._browser_host == "127.0.0.1"
        assert client._browser_port == 9222

    def test_passes_headless(self, mock_cookies: GrokCookies):
        """Headless mode is stored for browser initialization."""
        client = get_client(cookies=mock_cookies, headless=True)

        assert isinstance(client, SmartGrokClient)
        assert client._browser_headless is True

    def test_passes_config_path(self, mock_cookies: GrokCookies):
        """Config path is passed to client."""
        with patch("grok_web.client.load_config") as mock_load:
            mock_load.return_value = {"cookies": mock_cookies, "impersonate": "chrome136"}

            client = get_client(config_path="/custom/path.json")

            assert isinstance(client, SmartGrokClient)
            mock_load.assert_called_with("/custom/path.json")
