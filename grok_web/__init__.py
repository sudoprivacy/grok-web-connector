"""
Grok Web Connector - Python client for Grok Imagine web API.

    from grok_web import get_client

    async with get_client() as client:
        posts = await client.list_posts()
        video = await client.create_video({"images": ["post:" + post_id], "prompt": "zoom in"})

All APIs are public methods on GrokClient (see grok_web/client.py).
For parallel processing, see BrowserWorkerPool (grok_web/pool/).
"""

from pathlib import Path

from .agent_client import GrokAgentClient
from .auth import load_api_key, load_cookies, save_cookies
from .client import GrokClient
from .exceptions import (
    GrokAPIError,
    GrokAuthError,
    GrokConfigError,
    GrokError,
    GrokGenerationFailedError,
    GrokNotFoundError,
    GrokQuotaExceededError,
    GrokRateLimitError,
)
from .models import (
    MODE_IMG2VID,
    MODE_TXT2IMG,
    MODE_TXT2VID,
    MODE_UNKNOWN,
    MODE_UPLOAD2VID,
    AgentResponse,
    ChildPost,
    GrokCookies,
    ImageEditResult,
    ImageGenerationResult,
    ImageVideoMapping,
    PostDetails,
    PostSummary,
    VideoExtendResult,
    VideoGenerationResult,
    VideoMatchResult,
)
from .pool import BrowserWorkerPool
from .prompt_parser import classify_image_source, parse_prompt
from .schema import (
    AGENT_KEYS,
    ANIMATE_KEYS,
    API_EDIT_KEYS,
    API_IMAGE_KEYS,
    API_VIDEO_KEYS,
    EDIT_KEYS,
    EXTEND_KEYS,
    IMAGE_KEYS,
    PARAMS,
    REGENERATE_KEYS,
    UPLOAD_KEYS,
    VIDEO_KEYS,
    schema_to_docstring,
    schema_to_help,
    splice_schema_into_docstring,
    validate_params,
)
from .selectors import auto_favorite_first_n, select_all, signal_file_selector, timeout_selector
from .xai_client import XAIClient

try:
    from importlib.metadata import version

    __version__ = version("grok-web-connector")
except Exception:
    __version__ = "0.0.0"


def get_client(
    cookies: GrokCookies | None = None,
    config_path: Path | str | None = None,
    browser_host: str | None = None,
    browser_port: int | None = None,
    headless: bool = False,
    profile: str | None = None,
    startup_timeout: float = 30.0,
    extra_chrome_args: list[str] | None = None,
    user_data_dir: "str | Path | None" = None,
) -> GrokClient:
    """
    Get the Grok API client.

    This is the recommended entry point for all Grok operations.

    Args:
        cookies: Pre-loaded GrokCookies (optional, loads from config if None)
        config_path: Path to config file (default: ~/.grok-config.json)
        browser_host: Chrome debugging host (optional, defaults to 127.0.0.1)
        browser_port: Chrome debugging port (optional, defaults to 9350)
        headless: Run browser in headless mode (default: False)
        profile: Chrome profile name (optional, defaults to "grok-chrome")
        startup_timeout: Seconds to wait for Chrome to bind its debug port on
            auto-launch (default: 30.0). Raise on slow / crowded Windows
            machines or first-time profile init.
        extra_chrome_args: Additional Chrome command-line flags appended after
            ai-dev-browser's defaults and the connector's own
            ``--disable-logging`` / ``--log-file=NUL`` defaults (which silence
            Chrome's stderr so it doesn't fill the Popen pipe buffer and hang
            after a few minutes of CDP-heavy activity on Windows).

            Proxy users (China / restricted networks): Chrome launched by
            ai-dev-browser doesn't auto-inherit the Windows system proxy. If
            you get ``ERR_CONNECTION_CLOSED`` on grok.com even though
            ``curl https://grok.com`` works through Clash/V2Ray/Surge, pass
            the local proxy port here, e.g.
            ``extra_chrome_args=["--proxy-server=http://127.0.0.1:7897"]``.
        user_data_dir: Absolute path for Chrome's ``--user-data-dir``.
            Default: ``~/.grok-web-connector/profiles/<profile>/`` —
            DELIBERATELY OUTSIDE ai-dev-browser's managed namespace so other
            agents' ``browser_cleanup()`` calls cannot misclassify our Chrome
            as an orphan and kill it. Cross-agent safe by default.

    Returns:
        GrokClient instance with all API methods.

    Example:
        async with get_client() as client:
            posts = await client.list_posts()
            await client.favorite_post(posts[0].id)
            video = await client.create_video({"images": ["post:" + posts[0].id], "prompt": "zoom in"})
    """
    return GrokClient(
        cookies=cookies,
        config_path=config_path,
        host=browser_host,
        port=browser_port,
        headless=headless,
        profile=profile,
        startup_timeout=startup_timeout,
        extra_chrome_args=extra_chrome_args,
        user_data_dir=user_data_dir,
    )


def get_agent_client(
    cookies: GrokCookies | None = None,
    config_path: Path | str | None = None,
    browser_host: str | None = None,
    browser_port: int | None = None,
    headless: bool = False,
    profile: str | None = None,
    startup_timeout: float = 30.0,
    extra_chrome_args: list[str] | None = None,
    user_data_dir: "str | Path | None" = None,
) -> GrokAgentClient:
    """Get the Grok Agent Mode client.

    Use for conversational image/video generation on Agent Mode's
    infinite canvas. Same auth and browser lifecycle as get_client().

    Example:
        async with get_agent_client() as agent:
            r = await agent.send({"message": "create a logo for Bean Dream"})
            r2 = await agent.send({
                "message": "make it retro",
                "session_url": r.session_url,
            })
    """
    return GrokAgentClient(
        cookies=cookies,
        config_path=config_path,
        host=browser_host,
        port=browser_port,
        headless=headless,
        profile=profile,
        startup_timeout=startup_timeout,
        extra_chrome_args=extra_chrome_args,
        user_data_dir=user_data_dir,
    )


def get_api_client(
    api_key: str | None = None,
    config_path: Path | str | None = None,
) -> XAIClient:
    """Get the xAI REST API client for Grok Imagine.

    Use when you want image/video generation via xAI's official API
    without launching a browser. Requires XAI_API_KEY.

    API key resolution: api_key param → $XAI_API_KEY env var →
    "xai_api_key" in ~/.grok-config.json.

    Returns the same result types as get_client() (browser-based),
    enabling A/B comparison of moderation rates.

    Example:
        async with get_api_client() as client:
            result = await client.create_image({
                "prompt": "a cat",
                "model": "grok-imagine-image",
            })
    """
    return XAIClient(api_key=api_key, config_path=config_path)


__all__ = [
    # Factory functions (main entry points)
    "get_client",
    "get_agent_client",
    "get_api_client",
    # Client classes
    "GrokClient",
    "GrokAgentClient",
    "XAIClient",
    # Worker Pool (for parallel processing)
    "BrowserWorkerPool",
    # Schema (SSOT for params)
    "PARAMS",
    "VIDEO_KEYS",
    "IMAGE_KEYS",
    "EDIT_KEYS",
    "EXTEND_KEYS",
    "UPLOAD_KEYS",
    "ANIMATE_KEYS",
    "REGENERATE_KEYS",
    "AGENT_KEYS",
    "API_IMAGE_KEYS",
    "API_VIDEO_KEYS",
    "API_EDIT_KEYS",
    "schema_to_docstring",
    "schema_to_help",
    "splice_schema_into_docstring",
    "validate_params",
    # Prompt parser
    "parse_prompt",
    "classify_image_source",
    # Generation mode constants
    "MODE_TXT2IMG",
    "MODE_IMG2VID",
    "MODE_TXT2VID",
    "MODE_UPLOAD2VID",
    "MODE_UNKNOWN",
    # Models
    "AgentResponse",
    "PostSummary",
    "PostDetails",
    "ChildPost",
    "GrokCookies",
    "ImageEditResult",
    "ImageGenerationResult",
    "ImageVideoMapping",
    "VideoMatchResult",
    "VideoExtendResult",
    "VideoGenerationResult",
    # Exceptions
    "GrokError",
    "GrokAuthError",
    "GrokAPIError",
    "GrokNotFoundError",
    "GrokConfigError",
    "GrokRateLimitError",
    "GrokQuotaExceededError",
    "GrokGenerationFailedError",
    # Auth utilities
    "load_cookies",
    "save_cookies",
    "load_api_key",
    # Thumbnail selectors (for create_image)
    "select_all",
    "timeout_selector",
    "signal_file_selector",
    "auto_favorite_first_n",
]
