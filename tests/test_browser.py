"""Tests for browser management utilities."""

from unittest.mock import MagicMock, patch

import pytest

from grok_web.browser import (
    DEFAULT_DEBUG_HOST,
    DEFAULT_DEBUG_PORT,
    get_chrome_executable,
    is_port_in_use,
    launch_chrome_with_debug_port,
)


class TestGetChromeExecutable:
    """Tests for get_chrome_executable function."""

    def test_finds_chrome_on_macos(self):
        """Finds Chrome on macOS."""
        with patch("grok_web.browser.platform.system", return_value="Darwin"):
            with patch("grok_web.browser.Path.exists", return_value=True):
                result = get_chrome_executable()
                assert result == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    def test_finds_chrome_on_windows(self):
        """Finds Chrome on Windows."""
        with patch("grok_web.browser.platform.system", return_value="Windows"):
            with patch("grok_web.browser.Path.exists", return_value=True):
                result = get_chrome_executable()
                assert result == r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    def test_finds_chrome_on_linux(self):
        """Finds Chrome on Linux."""
        with patch("grok_web.browser.platform.system", return_value="Linux"):
            with patch("grok_web.browser.Path.exists", return_value=True):
                result = get_chrome_executable()
                assert result == "/usr/bin/google-chrome"

    def test_returns_none_when_not_found(self):
        """Returns None when Chrome not found."""
        with patch("grok_web.browser.platform.system", return_value="Darwin"):
            with patch("grok_web.browser.Path.exists", return_value=False):
                with patch("grok_web.browser.shutil.which", return_value=None):
                    result = get_chrome_executable()
                    assert result is None

    def test_falls_back_to_which_on_unix(self):
        """Falls back to 'which' command on Unix-like systems."""
        with patch("grok_web.browser.platform.system", return_value="Linux"):
            with patch("grok_web.browser.Path.exists", return_value=False):
                with patch("grok_web.browser.shutil.which", return_value="/usr/local/bin/chrome"):
                    result = get_chrome_executable()
                    assert result == "/usr/local/bin/chrome"


class TestIsPortInUse:
    """Tests for is_port_in_use function."""

    def test_returns_true_when_port_in_use(self):
        """Returns True when port is in use."""
        mock_socket = MagicMock()
        mock_socket.connect = MagicMock()

        with patch("grok_web.browser.socket.socket") as mock_socket_class:
            mock_socket_class.return_value.__enter__ = MagicMock(return_value=mock_socket)
            mock_socket_class.return_value.__exit__ = MagicMock(return_value=False)

            result = is_port_in_use("127.0.0.1", 9222)

            assert result is True

    def test_returns_false_when_connection_refused(self):
        """Returns False when connection is refused."""
        mock_socket = MagicMock()
        mock_socket.connect = MagicMock(side_effect=ConnectionRefusedError())

        with patch("grok_web.browser.socket.socket") as mock_socket_class:
            mock_socket_class.return_value.__enter__ = MagicMock(return_value=mock_socket)
            mock_socket_class.return_value.__exit__ = MagicMock(return_value=False)

            result = is_port_in_use("127.0.0.1", 9222)

            assert result is False

    def test_returns_false_on_timeout(self):
        """Returns False on socket timeout."""
        mock_socket = MagicMock()
        mock_socket.connect = MagicMock(side_effect=TimeoutError())

        with patch("grok_web.browser.socket.socket") as mock_socket_class:
            mock_socket_class.return_value.__enter__ = MagicMock(return_value=mock_socket)
            mock_socket_class.return_value.__exit__ = MagicMock(return_value=False)

            result = is_port_in_use("127.0.0.1", 9222)

            assert result is False

    def test_returns_false_on_os_error(self):
        """Returns False on OS error."""
        mock_socket = MagicMock()
        mock_socket.connect = MagicMock(side_effect=OSError())

        with patch("grok_web.browser.socket.socket") as mock_socket_class:
            mock_socket_class.return_value.__enter__ = MagicMock(return_value=mock_socket)
            mock_socket_class.return_value.__exit__ = MagicMock(return_value=False)

            result = is_port_in_use("127.0.0.1", 9222)

            assert result is False


class TestLaunchChromeWithDebugPort:
    """Tests for launch_chrome_with_debug_port function."""

    def test_raises_when_chrome_not_found(self):
        """Raises FileNotFoundError when Chrome not found."""
        with patch("grok_web.browser.get_chrome_executable", return_value=None):
            with pytest.raises(FileNotFoundError, match="Chrome executable not found"):
                launch_chrome_with_debug_port()

    def test_launches_chrome_with_correct_args(self):
        """Launches Chrome with correct arguments."""
        mock_process = MagicMock()

        with patch("grok_web.browser.get_chrome_executable", return_value="/path/to/chrome"):
            with patch(
                "grok_web.browser.subprocess.Popen", return_value=mock_process
            ) as mock_popen:
                with patch(
                    "grok_web.browser.tempfile.mkdtemp", return_value="/tmp/grok_chrome_123"
                ):
                    result = launch_chrome_with_debug_port(port=9222)

                    assert result == mock_process
                    call_args = mock_popen.call_args[0][0]
                    assert call_args[0] == "/path/to/chrome"
                    assert "--remote-debugging-port=9222" in call_args
                    assert "--user-data-dir=/tmp/grok_chrome_123" in call_args

    def test_launches_chrome_headless(self):
        """Launches Chrome in headless mode."""
        mock_process = MagicMock()

        with patch("grok_web.browser.get_chrome_executable", return_value="/path/to/chrome"):
            with patch(
                "grok_web.browser.subprocess.Popen", return_value=mock_process
            ) as mock_popen:
                with patch(
                    "grok_web.browser.tempfile.mkdtemp", return_value="/tmp/grok_chrome_123"
                ):
                    launch_chrome_with_debug_port(headless=True)

                    call_args = mock_popen.call_args[0][0]
                    assert "--headless=new" in call_args

    def test_uses_custom_user_data_dir(self):
        """Uses custom user data directory when provided."""
        mock_process = MagicMock()

        with patch("grok_web.browser.get_chrome_executable", return_value="/path/to/chrome"):
            with patch(
                "grok_web.browser.subprocess.Popen", return_value=mock_process
            ) as mock_popen:
                launch_chrome_with_debug_port(user_data_dir="/custom/path")

                call_args = mock_popen.call_args[0][0]
                assert "--user-data-dir=/custom/path" in call_args

    def test_raises_runtime_error_on_popen_failure(self):
        """Raises RuntimeError when Popen fails."""
        with patch("grok_web.browser.get_chrome_executable", return_value="/path/to/chrome"):
            with patch("grok_web.browser.subprocess.Popen", side_effect=Exception("spawn failed")):
                with patch(
                    "grok_web.browser.tempfile.mkdtemp", return_value="/tmp/grok_chrome_123"
                ):
                    with pytest.raises(RuntimeError, match="Failed to launch Chrome"):
                        launch_chrome_with_debug_port()


class TestEnsureChromeRunning:
    """Tests for ensure_chrome_running async function."""

    @pytest.mark.asyncio
    async def test_returns_none_when_chrome_already_running(self):
        """Returns None when Chrome is already running on port."""
        from grok_web.browser import ensure_chrome_running

        with patch("grok_web.browser.is_port_in_use", return_value=True):
            result = await ensure_chrome_running()
            assert result is None

    @pytest.mark.asyncio
    async def test_launches_chrome_when_not_running(self):
        """Launches Chrome when not running and returns process."""
        from grok_web.browser import ensure_chrome_running

        mock_process = MagicMock()
        port_check_calls = [False, False, True]  # Not running, then running

        with patch("grok_web.browser.is_port_in_use", side_effect=port_check_calls):
            with patch("grok_web.browser.launch_chrome_with_debug_port", return_value=mock_process):
                with patch("grok_web.browser.asyncio.sleep"):
                    result = await ensure_chrome_running()

                    assert result == mock_process

    @pytest.mark.asyncio
    async def test_raises_timeout_when_chrome_fails_to_start(self):
        """Raises TimeoutError when Chrome doesn't start in time."""
        from grok_web.browser import ensure_chrome_running

        mock_process = MagicMock()

        with patch("grok_web.browser.is_port_in_use", return_value=False):
            with patch("grok_web.browser.launch_chrome_with_debug_port", return_value=mock_process):
                with patch("grok_web.browser.asyncio.sleep"):
                    with pytest.raises(TimeoutError, match="did not start"):
                        await ensure_chrome_running(timeout=0.1)

                    mock_process.terminate.assert_called_once()


class TestDefaultConstants:
    """Tests for default constants."""

    def test_default_debug_port(self):
        """Default debug port is 9222."""
        assert DEFAULT_DEBUG_PORT == 9222

    def test_default_debug_host(self):
        """Default debug host is 127.0.0.1."""
        assert DEFAULT_DEBUG_HOST == "127.0.0.1"
