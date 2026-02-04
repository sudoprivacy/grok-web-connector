"""Tests for grok-specific browser management utilities.

Tests ai-dev-browser delegated functions are imported correctly,
and tests grok-specific functions (profile prefix, chrome detection).

Note: Low-level browser utilities (find_chrome, is_port_in_use, etc.)
are tested in ai-dev-browser's test suite.
"""

from unittest.mock import MagicMock, patch

import pytest

from grok_web.browser import (
    DEFAULT_DEBUG_HOST,
    DEFAULT_DEBUG_PORT,
    GROK_TEMP_PROFILE_PREFIX,
    # Backwards compatibility aliases
    TEMP_PROFILE_PREFIX,
    find_grok_chromes,
    find_nodriver_chromes,
    get_available_port,
    get_chrome_executable,
    is_grok_temp_chrome_on_port,
    is_port_in_use,
    is_temp_chrome_on_port,
    kill_stale_grok_chrome,
    kill_stale_temp_chrome,
    launch_chrome_with_debug_port,
    launch_grok_chrome,
)


class TestDefaultConstants:
    """Tests for default constants."""

    def test_default_debug_port_is_9350(self):
        """Default port should be 9350 (ai-dev-browser default)."""
        assert DEFAULT_DEBUG_PORT == 9350

    def test_default_debug_host_is_localhost(self):
        """Default host should be localhost."""
        assert DEFAULT_DEBUG_HOST == "127.0.0.1"

    def test_grok_temp_profile_prefix(self):
        """Grok temp profile prefix should be grok_chrome_."""
        assert GROK_TEMP_PROFILE_PREFIX == "grok_chrome_"


class TestBackwardsCompatibilityAliases:
    """Tests that backwards compatibility aliases exist and work."""

    def test_temp_profile_prefix_alias(self):
        """TEMP_PROFILE_PREFIX should alias GROK_TEMP_PROFILE_PREFIX."""
        assert TEMP_PROFILE_PREFIX == GROK_TEMP_PROFILE_PREFIX

    def test_find_nodriver_chromes_alias(self):
        """find_nodriver_chromes should alias find_grok_chromes."""
        assert find_nodriver_chromes is find_grok_chromes

    def test_is_temp_chrome_on_port_alias(self):
        """is_temp_chrome_on_port should alias is_grok_temp_chrome_on_port."""
        assert is_temp_chrome_on_port is is_grok_temp_chrome_on_port

    def test_kill_stale_temp_chrome_alias(self):
        """kill_stale_temp_chrome should alias kill_stale_grok_chrome."""
        assert kill_stale_temp_chrome is kill_stale_grok_chrome

    def test_launch_chrome_with_debug_port_alias(self):
        """launch_chrome_with_debug_port should alias launch_grok_chrome."""
        assert launch_chrome_with_debug_port is launch_grok_chrome

    def test_get_chrome_executable_exists(self):
        """get_chrome_executable should be importable."""
        assert callable(get_chrome_executable)

    def test_is_port_in_use_exists(self):
        """is_port_in_use should be importable."""
        assert callable(is_port_in_use)


class TestIsGrokTempChromeOnPort:
    """Tests for is_grok_temp_chrome_on_port function."""

    def test_returns_false_when_no_chrome(self):
        """Returns (False, None) when no Chrome on port."""
        with patch("grok_web.browser.get_pid_on_port", return_value=None):
            is_grok, pid = is_grok_temp_chrome_on_port(9350)
            assert is_grok is False
            assert pid is None

    def test_returns_false_when_not_grok_chrome(self):
        """Returns (False, pid) when Chrome exists but not grok profile."""
        with patch("grok_web.browser.get_pid_on_port", return_value=12345):
            with patch(
                "grok_web.browser.get_process_cmdline",
                return_value="/Applications/Google Chrome.app --user-data-dir=/tmp/other_chrome_123",
            ):
                is_grok, pid = is_grok_temp_chrome_on_port(9350)
                assert is_grok is False
                assert pid == 12345

    def test_returns_true_when_grok_chrome(self):
        """Returns (True, pid) when Chrome has grok_chrome_ prefix."""
        with patch("grok_web.browser.get_pid_on_port", return_value=12345):
            with patch(
                "grok_web.browser.get_process_cmdline",
                return_value=f"/Applications/Google Chrome.app --user-data-dir=/tmp/{GROK_TEMP_PROFILE_PREFIX}abc123",
            ):
                is_grok, pid = is_grok_temp_chrome_on_port(9350)
                assert is_grok is True
                assert pid == 12345

    def test_returns_false_when_cmdline_none(self):
        """Returns (False, pid) when cmdline cannot be retrieved."""
        with patch("grok_web.browser.get_pid_on_port", return_value=12345):
            with patch("grok_web.browser.get_process_cmdline", return_value=None):
                is_grok, pid = is_grok_temp_chrome_on_port(9350)
                assert is_grok is False
                assert pid == 12345


class TestFindGrokChromes:
    """Tests for find_grok_chromes function."""

    def test_finds_grok_chromes_in_range(self):
        """Finds all grok Chromes in the port range."""

        def mock_is_grok(port):
            return (port in [9351, 9353], port if port in [9351, 9353] else None)

        with patch("grok_web.browser.is_grok_temp_chrome_on_port", side_effect=mock_is_grok):
            with patch("grok_web.browser.is_chrome_in_use", return_value=False):
                ports = find_grok_chromes(port_range=(9350, 9355))
                assert ports == [9351, 9353]

    def test_excludes_in_use_chromes(self):
        """Excludes Chromes that are in use by another debugger."""

        def mock_is_grok(port):
            return (True, port)

        def mock_in_use(port):
            return port == 9352  # 9352 is in use

        with patch("grok_web.browser.is_grok_temp_chrome_on_port", side_effect=mock_is_grok):
            with patch("grok_web.browser.is_chrome_in_use", side_effect=mock_in_use):
                ports = find_grok_chromes(port_range=(9350, 9355), exclude_in_use=True)
                assert 9352 not in ports


class TestGetAvailablePort:
    """Tests for get_available_port function."""

    def test_returns_unused_port(self):
        """Returns first unused port when no grok Chromes available."""
        with patch("grok_web.browser.is_port_in_use", return_value=False):
            port = get_available_port(start=9350, end=9360)
            assert port == 9350

    def test_prefers_reusable_grok_chrome(self):
        """Prefers reusing idle grok Chrome over new port."""

        def mock_port_in_use(port):
            return port == 9351  # 9351 has Chrome

        def mock_is_grok(port):
            return (port == 9351, 9351 if port == 9351 else None)

        with patch("grok_web.browser.is_port_in_use", side_effect=mock_port_in_use):
            with patch("grok_web.browser.is_grok_temp_chrome_on_port", side_effect=mock_is_grok):
                with patch("grok_web.browser.is_chrome_in_use", return_value=False):
                    port = get_available_port(start=9350, end=9360)
                    assert port == 9351  # Reused grok Chrome

    def test_raises_when_no_port_available(self):
        """Raises RuntimeError when no port available."""
        with patch("grok_web.browser.is_port_in_use", return_value=True):
            with patch("grok_web.browser.is_grok_temp_chrome_on_port", return_value=(False, None)):
                with pytest.raises(RuntimeError, match="No available port"):
                    get_available_port(start=9350, end=9352)


class TestKillStaleGrokChrome:
    """Tests for kill_stale_grok_chrome function."""

    def test_kills_grok_chrome(self):
        """Kills grok Chrome and returns True."""
        with patch("grok_web.browser.is_grok_temp_chrome_on_port", return_value=(True, 12345)):
            with patch("grok_web.browser.os.kill") as mock_kill:
                result = kill_stale_grok_chrome(9350)
                assert result is True
                mock_kill.assert_called_once()

    def test_returns_false_when_not_grok_chrome(self):
        """Returns False when Chrome is not grok profile."""
        with patch("grok_web.browser.is_grok_temp_chrome_on_port", return_value=(False, 12345)):
            result = kill_stale_grok_chrome(9350)
            assert result is False

    def test_returns_false_when_no_chrome(self):
        """Returns False when no Chrome on port."""
        with patch("grok_web.browser.is_grok_temp_chrome_on_port", return_value=(False, None)):
            result = kill_stale_grok_chrome(9350)
            assert result is False


class TestLaunchGrokChrome:
    """Tests for launch_grok_chrome function."""

    def test_creates_grok_temp_dir_when_not_provided(self):
        """Creates temp dir with grok_chrome_ prefix when user_data_dir not provided."""
        with patch("grok_web.browser.tempfile.mkdtemp") as mock_mkdtemp:
            mock_mkdtemp.return_value = "/tmp/grok_chrome_abc123"
            with patch("grok_web.browser.launch_chrome") as mock_launch:
                mock_launch.return_value = MagicMock()
                launch_grok_chrome(port=9350)
                mock_mkdtemp.assert_called_once_with(prefix=GROK_TEMP_PROFILE_PREFIX)
                mock_launch.assert_called_once()

    def test_uses_provided_user_data_dir(self):
        """Uses provided user_data_dir instead of creating temp."""
        with patch("grok_web.browser.tempfile.mkdtemp") as mock_mkdtemp:
            with patch("grok_web.browser.launch_chrome") as mock_launch:
                mock_launch.return_value = MagicMock()
                launch_grok_chrome(port=9350, user_data_dir="/custom/path")
                mock_mkdtemp.assert_not_called()
                mock_launch.assert_called_once()
                call_kwargs = mock_launch.call_args[1]
                assert call_kwargs["user_data_dir"] == "/custom/path"
