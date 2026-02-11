"""Browser management utilities for NodriverClient.

Delegates to ai-dev-browser's start_browser() for Chrome lifecycle.
Uses named profiles for persistent Chrome sessions.
"""

import logging

from ai_dev_browser.core import start_browser
from ai_dev_browser.core.config import DEFAULT_DEBUG_HOST, DEFAULT_PORT_RANGE

logger = logging.getLogger(__name__)

# Named profile for grok Chrome (persistent across runs)
GROK_CHROME_PROFILE = "grok-chrome"

# Re-export from ai-dev-browser (SSOT)
DEFAULT_DEBUG_PORT = DEFAULT_PORT_RANGE[0]  # 9350


async def ensure_chrome_running(
    host: str = DEFAULT_DEBUG_HOST,  # noqa: ARG001 - kept for API compatibility
    port: int | None = None,
    headless: bool = False,
    timeout: float = 10.0,  # noqa: ARG001 - start_browser has its own timeout
    force_new: bool = False,
    profile: str | None = None,
):
    """Ensure Chrome is running with remote debugging.

    Delegates to ai-dev-browser's start_browser() for Chrome lifecycle.
    Uses named profile for automatic Chrome reuse and persistent sessions.

    Args:
        host: Remote debugging host (kept for API compat)
        port: Preferred debugging port (auto-assigned if None)
        headless: Run in headless mode if launching new instance
        timeout: Kept for API compat (start_browser has its own)
        force_new: If True, always launch new Chrome (reuse="none")
        profile: Chrome profile name (default: "grok-chrome")

    Returns:
        Tuple of (None, actual_port_used)
        Note: First element is always None since start_browser manages process.
    """
    profile_name = profile or GROK_CHROME_PROFILE

    kwargs = {
        "headless": headless,
        "profile": profile_name,
    }
    if port is not None:
        kwargs["port"] = port
    if force_new:
        kwargs["reuse"] = "none"

    result = start_browser(**kwargs)

    if "error" in result:
        raise RuntimeError(result["error"])

    actual_port = result["port"]
    if result.get("reused"):
        logger.info(f"Reusing Chrome on port {actual_port} (profile: {profile_name})")
    else:
        logger.info(f"Started new Chrome on port {actual_port} (profile: {profile_name})")

    return None, actual_port
