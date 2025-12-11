"""Tests for factory functions (get_client, get_sync_client)."""

from unittest.mock import patch

from grok_web import get_client, get_sync_client
from grok_web.client import (
    GrokClient,
    PlaywrightClient,
    SmartGrokClient,
)
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


class TestGetSyncClient:
    """Tests for get_sync_client() factory function."""

    def test_returns_playwright_client_when_available(self, mock_cookies: GrokCookies):
        """Returns PlaywrightClient when playwright is installed."""
        client = get_sync_client(cookies=mock_cookies)

        assert isinstance(client, PlaywrightClient)

    def test_returns_grok_client_when_playwright_unavailable(self, mock_cookies: GrokCookies):
        """Falls back to GrokClient when playwright not installed."""
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if "playwright" in name:
                raise ImportError("No module named 'playwright'")
            return original_import(name, *args, **kwargs)

        with patch("grok_web.client.load_config") as mock_load:
            mock_load.return_value = {
                "cookies": mock_cookies,
                "headers": {},
                "impersonate": "chrome136",
            }

            with patch.object(builtins, "__import__", mock_import):
                client = get_sync_client(cookies=mock_cookies)

                assert isinstance(client, GrokClient)

    def test_passes_config_path(self, mock_cookies: GrokCookies):
        """Config path is passed to client."""
        with patch("grok_web.client.load_config") as mock_load:
            mock_load.return_value = {"cookies": mock_cookies}

            client = get_sync_client(config_path="/custom/path.json")

            assert isinstance(client, PlaywrightClient)
            mock_load.assert_called_with("/custom/path.json")
