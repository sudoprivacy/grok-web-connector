"""Browser management utilities for NodriverClient.

Handles automatic Chrome launching with isolated profiles for reliable automation.
"""

import asyncio
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Default debugging port for Chrome
DEFAULT_DEBUG_PORT = 9222
DEFAULT_DEBUG_HOST = "127.0.0.1"

# Prefix used for temp Chrome profiles launched by this library
TEMP_PROFILE_PREFIX = "grok_chrome_"


def get_chrome_executable() -> str | None:
    """Find Chrome executable path based on platform.

    Returns:
        Path to Chrome executable, or None if not found.
    """
    system = platform.system()

    if system == "Darwin":  # macOS
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
        ]
    else:  # Linux
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]

    # Check which candidates
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate

    # Try to find via 'which' on Unix-like systems
    if system != "Windows":
        for cmd in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
            result = shutil.which(cmd)
            if result:
                return result

    return None


def is_port_in_use(host: str, port: int) -> bool:
    """Check if a port is in use (Chrome might be listening).

    Args:
        host: Host to check
        port: Port to check

    Returns:
        True if port is in use, False otherwise.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect((host, port))
            return True
        except (TimeoutError, ConnectionRefusedError, OSError):
            return False


def get_pid_on_port(port: int) -> int | None:
    """Get the PID of the process listening on a port.

    Args:
        port: Port number to check

    Returns:
        PID if found, None otherwise.
    """
    system = platform.system()

    if system == "Darwin" or system == "Linux":
        # Use lsof on Unix-like systems
        try:
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-t", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                # lsof -t returns just the PID
                return int(result.stdout.strip().split("\n")[0])
        except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
            pass
    elif system == "Windows":
        # Use netstat on Windows
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.split("\n"):
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        return int(parts[-1])
        except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
            pass

    return None


def get_process_cmdline(pid: int) -> str | None:
    """Get the command line arguments of a process.

    Args:
        pid: Process ID

    Returns:
        Command line string if found, None otherwise.
    """
    system = platform.system()

    if system == "Darwin" or system == "Linux":
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    elif system == "Windows":
        try:
            result = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                lines = [line.strip() for line in result.stdout.split("\n") if line.strip()]
                if len(lines) > 1:
                    return lines[1]  # Skip header
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return None


def is_temp_chrome_on_port(port: int) -> tuple[bool, int | None]:
    """Check if the Chrome on a port is a temp profile launched by us.

    Args:
        port: Port to check

    Returns:
        Tuple of (is_temp_chrome, pid). If not a temp Chrome or no Chrome, returns (False, None).
    """
    pid = get_pid_on_port(port)
    if pid is None:
        return False, None

    cmdline = get_process_cmdline(pid)
    if cmdline is None:
        return False, pid

    # Check if it's Chrome with our temp profile prefix
    if "chrome" in cmdline.lower() and TEMP_PROFILE_PREFIX in cmdline:
        return True, pid

    return False, pid


def kill_stale_temp_chrome(port: int) -> bool:
    """Kill a stale temp Chrome process on the given port.

    Only kills Chrome if it was launched by us (has grok_chrome_ temp profile).

    Args:
        port: Port where Chrome might be listening

    Returns:
        True if a temp Chrome was killed, False otherwise.
    """
    is_temp, pid = is_temp_chrome_on_port(port)

    if not is_temp or pid is None:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        logger.info(f"Killed stale temp Chrome (PID {pid}) on port {port}")
        return True
    except (ProcessLookupError, PermissionError) as e:
        logger.warning(f"Failed to kill temp Chrome (PID {pid}): {e}")
        return False


def launch_chrome_with_debug_port(
    port: int = DEFAULT_DEBUG_PORT,
    headless: bool = False,
    user_data_dir: str | Path | None = None,
) -> subprocess.Popen:
    """Launch Chrome with remote debugging enabled.

    Args:
        port: Remote debugging port (default: 9222)
        headless: Run in headless mode
        user_data_dir: Custom user data directory. If None, creates a temp directory.

    Returns:
        Popen process handle for the Chrome instance.

    Raises:
        FileNotFoundError: If Chrome executable not found.
        RuntimeError: If Chrome fails to start.
    """
    chrome_path = get_chrome_executable()
    if not chrome_path:
        raise FileNotFoundError(
            "Chrome executable not found. Please install Google Chrome or set the path manually."
        )

    # Create isolated user data directory if not provided
    if user_data_dir is None:
        user_data_dir = tempfile.mkdtemp(prefix="grok_chrome_")

    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-client-side-phishing-detection",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-hang-monitor",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--safebrowsing-disable-auto-update",
    ]

    if headless:
        args.append("--headless=new")

    # Start Chrome process
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detach from parent process
        )
    except Exception as e:
        raise RuntimeError(f"Failed to launch Chrome: {e}") from e

    return process


async def ensure_chrome_running(
    host: str = DEFAULT_DEBUG_HOST,
    port: int = DEFAULT_DEBUG_PORT,
    headless: bool = False,
    timeout: float = 10.0,
) -> subprocess.Popen | None:
    """Ensure Chrome is running with remote debugging.

    If a user's Chrome (real profile) is running on the port, reuses it.
    If a stale temp Chrome (from previous NodriverClient) is on the port, kills it
    and launches a fresh one.
    Otherwise, launches a new Chrome instance.

    Args:
        host: Remote debugging host
        port: Remote debugging port
        headless: Run in headless mode if launching new instance
        timeout: Max seconds to wait for Chrome to start

    Returns:
        Popen process if we launched Chrome, None if reusing existing Chrome.

    Raises:
        TimeoutError: If Chrome doesn't start within timeout.
        FileNotFoundError: If Chrome executable not found.
    """
    # Check if Chrome is already running
    if is_port_in_use(host, port):
        # Check if it's a stale temp Chrome from a previous session
        is_temp, pid = is_temp_chrome_on_port(port)
        if is_temp:
            logger.info(f"Found stale temp Chrome (PID {pid}) on port {port}, killing it")
            kill_stale_temp_chrome(port)
            # Wait for port to be released
            for _ in range(10):
                if not is_port_in_use(host, port):
                    break
                await asyncio.sleep(0.2)
        else:
            # User's Chrome with real profile - reuse it
            logger.debug(f"Reusing existing Chrome on port {port}")
            return None

    # Launch Chrome
    process = launch_chrome_with_debug_port(port=port, headless=headless)

    # Wait for Chrome to be ready
    start_time = asyncio.get_event_loop().time()
    while not is_port_in_use(host, port):
        if asyncio.get_event_loop().time() - start_time > timeout:
            process.terminate()
            raise TimeoutError(f"Chrome did not start within {timeout} seconds")
        await asyncio.sleep(0.2)

    # Give Chrome a moment to fully initialize
    await asyncio.sleep(0.5)

    return process
