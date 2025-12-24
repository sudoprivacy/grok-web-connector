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
    async def test_returns_none_when_user_chrome_already_running(self):
        """Returns None when user's Chrome (real profile) is already running on port."""
        from grok_web.browser import ensure_chrome_running

        with patch("grok_web.browser.is_port_in_use", return_value=True):
            # Not a temp Chrome (user's real Chrome)
            with patch("grok_web.browser.is_temp_chrome_on_port", return_value=(False, 12345)):
                result_tuple = await ensure_chrome_running()
                # Function now returns (process, port) tuple
                assert result_tuple == (None, 9222)

    @pytest.mark.asyncio
    async def test_reuses_temp_chrome_when_running(self):
        """Reuses existing temp Chrome to preserve logged-in session."""
        from grok_web.browser import ensure_chrome_running

        with patch("grok_web.browser.is_port_in_use", return_value=True):
            # It's a temp Chrome from previous session
            with patch("grok_web.browser.is_temp_chrome_on_port", return_value=(True, 99999)):
                process, port = await ensure_chrome_running()
                # Should reuse (return None) instead of killing and launching new
                assert process is None
                assert port == 9222

    @pytest.mark.asyncio
    async def test_launches_chrome_when_not_running(self):
        """Launches Chrome when not running and returns process."""
        from grok_web.browser import ensure_chrome_running

        mock_process = MagicMock()
        mock_process.poll.return_value = None  # Process still running
        mock_process.pid = 12345
        port_check_calls = [False, False, True]  # Not running, then running

        with patch("grok_web.browser.is_port_in_use", side_effect=port_check_calls):
            with patch("grok_web.browser.launch_chrome_with_debug_port", return_value=mock_process):
                with patch("grok_web.browser.asyncio.sleep"):
                    with patch("grok_web.browser.platform.system", return_value="Darwin"):
                        process, port = await ensure_chrome_running()
                        assert process == mock_process
                        assert port == 9222

    @pytest.mark.asyncio
    async def test_raises_timeout_when_chrome_fails_to_start(self):
        """Raises TimeoutError when Chrome doesn't start in time."""
        from grok_web.browser import ensure_chrome_running

        mock_process = MagicMock()
        mock_process.poll.return_value = None  # Process still running
        mock_process.pid = 12345

        with patch("grok_web.browser.is_port_in_use", return_value=False):
            with patch("grok_web.browser.launch_chrome_with_debug_port", return_value=mock_process):
                with patch("grok_web.browser.asyncio.sleep"):
                    with patch("grok_web.browser.platform.system", return_value="Darwin"):
                        with pytest.raises(TimeoutError, match="failed to start"):
                            await ensure_chrome_running(timeout=0.1)


class TestDefaultConstants:
    """Tests for default constants."""

    def test_default_debug_port(self):
        """Default debug port is 9222."""
        assert DEFAULT_DEBUG_PORT == 9222

    def test_default_debug_host(self):
        """Default debug host is 127.0.0.1."""
        assert DEFAULT_DEBUG_HOST == "127.0.0.1"


class TestFindNodriverChromes:
    """Tests for find_nodriver_chromes function."""

    def test_finds_nodriver_chromes_in_range(self):
        """Finds nodriver Chrome instances in port range."""
        from grok_web.browser import find_nodriver_chromes

        # Mock is_temp_chrome_on_port to return True for specific ports
        def mock_is_temp(port):
            if port in [9223, 9225]:
                return (True, port * 10)  # fake PID
            return (False, None)

        with patch("grok_web.browser.is_temp_chrome_on_port", side_effect=mock_is_temp):
            result = find_nodriver_chromes(port_range=(9222, 9227))
            assert result == [9223, 9225]

    def test_returns_empty_list_when_no_nodriver_chromes(self):
        """Returns empty list when no nodriver Chromes found."""
        from grok_web.browser import find_nodriver_chromes

        with patch("grok_web.browser.is_temp_chrome_on_port", return_value=(False, None)):
            result = find_nodriver_chromes(port_range=(9222, 9225))
            assert result == []

    def test_uses_default_port_range(self):
        """Uses default port range when not specified."""
        from grok_web.browser import find_nodriver_chromes

        call_count = 0

        def mock_is_temp(port):
            nonlocal call_count
            call_count += 1
            return (False, None)

        with patch("grok_web.browser.is_temp_chrome_on_port", side_effect=mock_is_temp):
            find_nodriver_chromes()
            # Default range is 9222-9300, so 78 calls
            assert call_count == 78


class TestGetAvailablePort:
    """Tests for get_available_port function."""

    def test_returns_first_available_port(self):
        """Returns first available port in range."""
        from grok_web.browser import get_available_port

        # Port 9222 in use, 9223 free
        def mock_port_in_use(host, port):
            return port == 9222

        with patch("grok_web.browser.is_port_in_use", side_effect=mock_port_in_use):
            result = get_available_port(start=9222, end=9230)
            assert result == 9223

    def test_skips_excluded_ports(self):
        """Skips ports in exclude set."""
        from grok_web.browser import get_available_port

        with patch("grok_web.browser.is_port_in_use", return_value=False):
            result = get_available_port(start=9222, end=9230, exclude={9222, 9223})
            assert result == 9224

    def test_raises_when_no_port_available(self):
        """Raises RuntimeError when no port available in range."""
        from grok_web.browser import get_available_port

        with patch("grok_web.browser.is_port_in_use", return_value=True):
            with pytest.raises(RuntimeError, match="No available port found"):
                get_available_port(start=9222, end=9225)

    def test_returns_first_port_when_all_free(self):
        """Returns first port when all are free."""
        from grok_web.browser import get_available_port

        with patch("grok_web.browser.is_port_in_use", return_value=False):
            result = get_available_port(start=9222, end=9230)
            assert result == 9222


class TestIsTempChromeOnPort:
    """Tests for is_temp_chrome_on_port function."""

    def test_returns_false_when_no_process_on_port(self):
        """Returns (False, None) when no process on port."""
        from grok_web.browser import is_temp_chrome_on_port

        with patch("grok_web.browser.get_pid_on_port", return_value=None):
            is_temp, pid = is_temp_chrome_on_port(9222)
            assert is_temp is False
            assert pid is None

    def test_returns_false_when_not_chrome(self):
        """Returns (False, pid) when process is not Chrome."""
        from grok_web.browser import is_temp_chrome_on_port

        with patch("grok_web.browser.get_pid_on_port", return_value=12345):
            with patch("grok_web.browser.get_process_cmdline", return_value="python server.py"):
                is_temp, pid = is_temp_chrome_on_port(9222)
                assert is_temp is False
                assert pid == 12345

    def test_returns_false_when_user_chrome(self):
        """Returns (False, pid) when Chrome without temp profile prefix."""
        from grok_web.browser import is_temp_chrome_on_port

        with patch("grok_web.browser.get_pid_on_port", return_value=12345):
            with patch(
                "grok_web.browser.get_process_cmdline",
                return_value="/Applications/Chrome.app --user-data-dir=/Users/me/Library/Chrome",
            ):
                is_temp, pid = is_temp_chrome_on_port(9222)
                assert is_temp is False
                assert pid == 12345

    def test_returns_true_when_temp_chrome(self):
        """Returns (True, pid) when Chrome with grok_chrome_ prefix."""
        from grok_web.browser import is_temp_chrome_on_port

        with patch("grok_web.browser.get_pid_on_port", return_value=12345):
            with patch(
                "grok_web.browser.get_process_cmdline",
                return_value="/Applications/Chrome.app --user-data-dir=/tmp/grok_chrome_abc123",
            ):
                is_temp, pid = is_temp_chrome_on_port(9222)
                assert is_temp is True
                assert pid == 12345

    def test_returns_false_when_cmdline_none(self):
        """Returns (False, pid) when cmdline cannot be retrieved."""
        from grok_web.browser import is_temp_chrome_on_port

        with patch("grok_web.browser.get_pid_on_port", return_value=12345):
            with patch("grok_web.browser.get_process_cmdline", return_value=None):
                is_temp, pid = is_temp_chrome_on_port(9222)
                assert is_temp is False
                assert pid == 12345


class TestKillStaleTempChrome:
    """Tests for kill_stale_temp_chrome function."""

    def test_returns_false_when_not_temp_chrome(self):
        """Returns False when process is not a temp Chrome."""
        from grok_web.browser import kill_stale_temp_chrome

        with patch("grok_web.browser.is_temp_chrome_on_port", return_value=(False, None)):
            result = kill_stale_temp_chrome(9222)
            assert result is False

    def test_kills_temp_chrome_and_returns_true(self):
        """Kills temp Chrome and returns True."""
        from grok_web.browser import kill_stale_temp_chrome

        with patch("grok_web.browser.is_temp_chrome_on_port", return_value=(True, 12345)):
            with patch("grok_web.browser.os.kill") as mock_kill:
                result = kill_stale_temp_chrome(9222)
                assert result is True
                mock_kill.assert_called_once()

    def test_returns_false_on_kill_error(self):
        """Returns False when kill fails."""
        from grok_web.browser import kill_stale_temp_chrome

        with patch("grok_web.browser.is_temp_chrome_on_port", return_value=(True, 12345)):
            with patch("grok_web.browser.os.kill", side_effect=ProcessLookupError()):
                result = kill_stale_temp_chrome(9222)
                assert result is False


class TestGetPidOnPort:
    """Tests for get_pid_on_port function."""

    def test_unix_lsof_returns_pid(self):
        """Returns PID from lsof on Unix systems."""
        from grok_web.browser import get_pid_on_port

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "12345\n"

        with patch("grok_web.browser.platform.system", return_value="Darwin"):
            with patch("grok_web.browser.subprocess.run", return_value=mock_result):
                result = get_pid_on_port(9222)
                assert result == 12345

    def test_unix_lsof_returns_none_on_error(self):
        """Returns None when lsof fails on Unix."""
        from grok_web.browser import get_pid_on_port

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("grok_web.browser.platform.system", return_value="Linux"):
            with patch("grok_web.browser.subprocess.run", return_value=mock_result):
                result = get_pid_on_port(9222)
                assert result is None

    def test_windows_netstat_returns_pid(self):
        """Returns PID from netstat on Windows."""
        from grok_web.browser import get_pid_on_port

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "  TCP    127.0.0.1:9222    0.0.0.0:0    LISTENING    12345\n"

        with patch("grok_web.browser.platform.system", return_value="Windows"):
            with patch("grok_web.browser.subprocess.run", return_value=mock_result):
                result = get_pid_on_port(9222)
                assert result == 12345

    def test_returns_none_on_subprocess_timeout(self):
        """Returns None on subprocess timeout."""
        from subprocess import TimeoutExpired

        from grok_web.browser import get_pid_on_port

        with patch("grok_web.browser.platform.system", return_value="Darwin"):
            with patch("grok_web.browser.subprocess.run", side_effect=TimeoutExpired("cmd", 5)):
                result = get_pid_on_port(9222)
                assert result is None


class TestGetProcessCmdline:
    """Tests for get_process_cmdline function."""

    def test_unix_ps_returns_cmdline(self):
        """Returns cmdline from ps on Unix systems."""
        from grok_web.browser import get_process_cmdline

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "/Applications/Chrome.app --remote-debugging-port=9222\n"

        with patch("grok_web.browser.platform.system", return_value="Darwin"):
            with patch("grok_web.browser.subprocess.run", return_value=mock_result):
                result = get_process_cmdline(12345)
                assert "Chrome.app" in result

    def test_windows_wmic_returns_cmdline(self):
        """Returns cmdline from wmic on Windows."""
        from grok_web.browser import get_process_cmdline

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "CommandLine\nchrome.exe --remote-debugging-port=9222\n"

        with patch("grok_web.browser.platform.system", return_value="Windows"):
            with patch("grok_web.browser.subprocess.run", return_value=mock_result):
                result = get_process_cmdline(12345)
                assert "chrome.exe" in result

    def test_returns_none_on_subprocess_timeout(self):
        """Returns None on subprocess timeout."""
        from subprocess import TimeoutExpired

        from grok_web.browser import get_process_cmdline

        with patch("grok_web.browser.platform.system", return_value="Linux"):
            with patch("grok_web.browser.subprocess.run", side_effect=TimeoutExpired("cmd", 5)):
                result = get_process_cmdline(12345)
                assert result is None



# Note: File-based port locking (TestPortLock) was removed in favor of CDP-based detection.
# CDP detection is more reliable: no stale locks, automatic cleanup when process dies.
