"""Tests for browser management utilities.

Tests ensure_chrome_running() delegation to ai-dev-browser's start_browser().
"""

from unittest.mock import patch

import pytest

from grok_web.browser import (
    DEFAULT_DEBUG_HOST,
    DEFAULT_DEBUG_PORT,
    GROK_CHROME_PROFILE,
    ensure_chrome_running,
)


class TestDefaultConstants:
    """Tests for default constants."""

    def test_default_debug_port_is_9350(self):
        """Default port should be 9350 (ai-dev-browser default)."""
        assert DEFAULT_DEBUG_PORT == 9350

    def test_default_debug_host_is_localhost(self):
        """Default host should be localhost."""
        assert DEFAULT_DEBUG_HOST == "127.0.0.1"

    def test_grok_chrome_profile(self):
        """Grok Chrome profile should be 'grok-chrome'."""
        assert GROK_CHROME_PROFILE == "grok-chrome"


class TestEnsureChromeRunning:
    """Tests for ensure_chrome_running function."""

    @pytest.mark.asyncio
    async def test_delegates_to_start_browser(self):
        """ensure_chrome_running should call start_browser with correct args."""
        mock_result = {"port": 9350, "pid": 12345, "reused": False}
        with patch("grok_web.browser.start_browser", return_value=mock_result) as mock_start:
            process, port = await ensure_chrome_running()
            assert process is None
            assert port == 9350
            mock_start.assert_called_once_with(
                headless=False,
                profile="grok-chrome",
            )

    @pytest.mark.asyncio
    async def test_passes_port_when_specified(self):
        """Should pass port to start_browser when specified."""
        mock_result = {"port": 9355, "pid": 12345, "reused": False}
        with patch("grok_web.browser.start_browser", return_value=mock_result) as mock_start:
            _, port = await ensure_chrome_running(port=9355)
            assert port == 9355
            mock_start.assert_called_once_with(
                headless=False,
                profile="grok-chrome",
                port=9355,
            )

    @pytest.mark.asyncio
    async def test_force_new_sets_reuse_none(self):
        """force_new=True should set reuse='none'."""
        mock_result = {"port": 9350, "pid": 12345, "reused": False}
        with patch("grok_web.browser.start_browser", return_value=mock_result) as mock_start:
            await ensure_chrome_running(force_new=True)
            mock_start.assert_called_once_with(
                headless=False,
                profile="grok-chrome",
                reuse="none",
            )

    @pytest.mark.asyncio
    async def test_custom_profile(self):
        """Should pass custom profile to start_browser."""
        mock_result = {"port": 9351, "pid": 12345, "reused": True}
        with patch("grok_web.browser.start_browser", return_value=mock_result) as mock_start:
            _, port = await ensure_chrome_running(profile="grok-chrome-w0")
            assert port == 9351
            mock_start.assert_called_once_with(
                headless=False,
                profile="grok-chrome-w0",
            )

    @pytest.mark.asyncio
    async def test_raises_on_error(self):
        """Should raise RuntimeError when start_browser returns error."""
        mock_result = {"error": "No available port"}
        with patch("grok_web.browser.start_browser", return_value=mock_result):
            with pytest.raises(RuntimeError, match="No available port"):
                await ensure_chrome_running()

    @pytest.mark.asyncio
    async def test_reused_chrome(self):
        """Should return None process when Chrome is reused."""
        mock_result = {"port": 9350, "pid": 12345, "reused": True}
        with patch("grok_web.browser.start_browser", return_value=mock_result):
            process, port = await ensure_chrome_running()
            assert process is None
            assert port == 9350
