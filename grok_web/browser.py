"""Browser management utilities for NodriverClient.

Handles automatic Chrome launching with isolated profiles for reliable automation.
"""

import asyncio
import platform
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

# Default debugging port for Chrome
DEFAULT_DEBUG_PORT = 9222
DEFAULT_DEBUG_HOST = "127.0.0.1"


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

    If Chrome is already running on the specified port, returns None.
    Otherwise, launches a new Chrome instance and waits for it to be ready.

    Args:
        host: Remote debugging host
        port: Remote debugging port
        headless: Run in headless mode if launching new instance
        timeout: Max seconds to wait for Chrome to start

    Returns:
        Popen process if we launched Chrome, None if Chrome was already running.

    Raises:
        TimeoutError: If Chrome doesn't start within timeout.
        FileNotFoundError: If Chrome executable not found.
    """
    # Check if Chrome is already running
    if is_port_in_use(host, port):
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
