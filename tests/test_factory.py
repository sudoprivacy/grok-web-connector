"""Tests for factory function (get_client)."""

from grok_web import get_client
from grok_web.client import GrokClient
from grok_web.models import GrokCookies


class TestGetClient:
    """Tests for get_client() factory function."""

    def test_returns_grok_client(self, mock_cookies: GrokCookies):
        """Returns GrokClient."""
        client = get_client(cookies=mock_cookies)

        assert isinstance(client, GrokClient)

    def test_passes_browser_host_port(self, mock_cookies: GrokCookies):
        """Browser host and port are stored for lazy initialization."""
        client = get_client(
            cookies=mock_cookies,
            browser_host="127.0.0.1",
            browser_port=9350,
        )

        assert isinstance(client, GrokClient)
        assert client._remote_host == "127.0.0.1"
        assert client._remote_port == 9350

    def test_passes_headless(self, mock_cookies: GrokCookies):
        """Headless mode is stored for browser initialization."""
        client = get_client(cookies=mock_cookies, headless=True)

        assert isinstance(client, GrokClient)
        assert client._headless is True

    def test_passes_config_path(self, mock_cookies: GrokCookies):
        """Config path is stored for deferred loading."""
        from pathlib import Path

        client = get_client(config_path="/custom/path.json", cookies=mock_cookies)

        assert isinstance(client, GrokClient)
        assert client._config_path == Path("/custom/path.json")
