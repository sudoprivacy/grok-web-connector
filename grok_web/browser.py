"""Browser management utilities for NodriverClient.

Delegates to ai-dev-browser for common browser operations.
Keeps grok-specific profile prefix for temp Chrome identification.
"""

import asyncio
import logging
import os
import signal
import tempfile
from pathlib import Path

# Import from ai-dev-browser instead of duplicating code
from ai_dev_browser import (
    find_chrome,
    get_pid_on_port,
    get_process_cmdline,
    is_chrome_in_use,
    is_port_in_use,
    launch_chrome,
)
from ai_dev_browser.core.config import DEFAULT_DEBUG_HOST, DEFAULT_PORT_RANGE

logger = logging.getLogger(__name__)

# Grok-specific temp profile prefix (different from ai-dev-browser's)
GROK_TEMP_PROFILE_PREFIX = "grok_chrome_"

# Re-export for backwards compatibility
DEFAULT_DEBUG_PORT = DEFAULT_PORT_RANGE[0]  # 9350


def is_grok_temp_chrome_on_port(port: int) -> tuple[bool, int | None]:
    """Check if the Chrome on a port is a grok temp profile.

    Args:
        port: Port to check

    Returns:
        Tuple of (is_grok_temp_chrome, pid).
    """
    pid = get_pid_on_port(port)
    if pid is None:
        return False, None

    cmdline = get_process_cmdline(pid)
    if cmdline is None:
        return False, pid

    # Check if it's Chrome with our grok temp profile prefix
    if "chrome" in cmdline.lower() and GROK_TEMP_PROFILE_PREFIX in cmdline:
        return True, pid

    return False, pid


def find_grok_chromes(
    port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
    exclude_in_use: bool = True,
) -> list[int]:
    """Find all ports with grok temp Chrome instances.

    Args:
        port_range: Tuple of (start_port, end_port) to scan
        exclude_in_use: If True, skip ports with attached debugger sessions

    Returns:
        List of ports with grok Chrome instances.
    """
    grok_ports = []
    for port in range(port_range[0], port_range[1]):
        is_grok, _ = is_grok_temp_chrome_on_port(port)
        if is_grok:
            if exclude_in_use and is_chrome_in_use(port):
                logger.debug(f"Skipping in-use Chrome on port {port}")
                continue
            grok_ports.append(port)
    return grok_ports


def get_available_port(
    start: int = DEFAULT_PORT_RANGE[0],
    end: int = DEFAULT_PORT_RANGE[1],
    exclude: set[int] | None = None,
) -> int:
    """Find an available port, preferring to reuse existing grok Chromes.

    Args:
        start: Start of port range
        end: End of port range
        exclude: Ports to skip

    Returns:
        Available port number

    Raises:
        RuntimeError: If no available port found
    """
    exclude = exclude or set()

    # Strategy 1: Reuse existing grok Chrome not in use
    for port in range(start, end):
        if port in exclude:
            continue
        if is_port_in_use(port=port):
            is_grok, _ = is_grok_temp_chrome_on_port(port)
            if is_grok and not is_chrome_in_use(port):
                logger.debug(f"Found reusable grok Chrome on port {port}")
                return port

    # Strategy 2: Find unused port
    for port in range(start, end):
        if port in exclude:
            continue
        if not is_port_in_use(port=port):
            return port

    raise RuntimeError(f"No available port found in range {start}-{end}")


def kill_stale_grok_chrome(port: int) -> bool:
    """Kill a stale grok temp Chrome on the given port.

    Args:
        port: Port where Chrome might be listening

    Returns:
        True if killed, False otherwise.
    """
    is_grok, pid = is_grok_temp_chrome_on_port(port)

    if not is_grok or pid is None:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        logger.info(f"Killed stale grok Chrome (PID {pid}) on port {port}")
        return True
    except (ProcessLookupError, PermissionError) as e:
        logger.warning(f"Failed to kill grok Chrome (PID {pid}): {e}")
        return False


def launch_grok_chrome(
    port: int = DEFAULT_DEBUG_PORT,
    headless: bool = False,
    user_data_dir: str | Path | None = None,
):
    """Launch Chrome with grok temp profile.

    Args:
        port: Remote debugging port
        headless: Run in headless mode
        user_data_dir: Custom user data dir. If None, creates grok temp dir.

    Returns:
        Popen process handle
    """
    # Create grok-specific temp directory if not provided
    if user_data_dir is None:
        user_data_dir = tempfile.mkdtemp(prefix=GROK_TEMP_PROFILE_PREFIX)

    return launch_chrome(
        port=port,
        headless=headless,
        start_url="about:blank",
        user_data_dir=str(user_data_dir),
    )


async def ensure_chrome_running(
    host: str = DEFAULT_DEBUG_HOST,  # noqa: ARG001 - kept for API compatibility
    port: int = DEFAULT_DEBUG_PORT,
    headless: bool = False,
    timeout: float = 10.0,
    force_new: bool = False,
):
    """Ensure Chrome is running with remote debugging.

    Args:
        host: Remote debugging host
        port: Remote debugging port
        headless: Run in headless mode if launching new instance
        timeout: Max seconds to wait for Chrome to start
        force_new: If True, always launch new Chrome

    Returns:
        Tuple of (Popen process or None, actual_port_used)
    """
    port_range = (port, port + 100)

    if force_new:
        # Find unused port
        for candidate in range(port, port + 100):
            if not is_port_in_use(port=candidate):
                port = candidate
                break
        else:
            raise RuntimeError(f"No available ports in range {port}-{port+99}")

    elif is_port_in_use(port=port):
        if is_chrome_in_use(port):
            # Port busy, find available one
            logger.warning(f"Chrome on port {port} is in use, searching...")

            # Look for idle grok Chrome or unused port
            unused_port = None
            for candidate in range(port_range[0], port_range[1]):
                if candidate == port:
                    continue
                if not is_port_in_use(port=candidate):
                    if unused_port is None:
                        unused_port = candidate
                    continue
                is_grok, _ = is_grok_temp_chrome_on_port(candidate)
                if is_grok and not is_chrome_in_use(candidate):
                    logger.info(f"Reusing idle grok Chrome on port {candidate}")
                    return None, candidate

            if unused_port is not None:
                port = unused_port
            else:
                raise RuntimeError(f"No available ports in range {port_range[0]}-{port_range[1]}")
        else:
            # Port has Chrome but not attached - reuse it
            is_grok, pid = is_grok_temp_chrome_on_port(port)
            if is_grok:
                logger.debug(f"Reusing grok Chrome (PID {pid}) on port {port}")
            else:
                logger.debug(f"Reusing existing Chrome on port {port}")
            return None, port

    # Launch new Chrome
    process = launch_grok_chrome(port=port, headless=headless)

    # Wait for Chrome to be ready
    start_time = asyncio.get_event_loop().time()
    while not is_port_in_use(port=port):
        elapsed = asyncio.get_event_loop().time() - start_time
        poll_result = process.poll()
        if poll_result is not None:
            raise RuntimeError(f"Chrome process exited with code {poll_result}")
        if elapsed > timeout:
            raise TimeoutError(f"Chrome failed to start on port {port} within {timeout}s")
        await asyncio.sleep(0.2)

    await asyncio.sleep(0.5)
    return process, port


# Backwards compatibility re-exports
get_chrome_executable = find_chrome
is_temp_chrome_on_port = is_grok_temp_chrome_on_port
find_nodriver_chromes = find_grok_chromes
kill_stale_temp_chrome = kill_stale_grok_chrome
launch_chrome_with_debug_port = launch_grok_chrome
TEMP_PROFILE_PREFIX = GROK_TEMP_PROFILE_PREFIX
