"""Authentication and configuration management for Grok Web Connector."""

import json
import platform
from pathlib import Path
from typing import Any

from .exceptions import GrokConfigError
from .models import GrokCookies

DEFAULT_CONFIG_PATH = Path.home() / ".grok-config.json"

# Default Chrome version for TLS fingerprint impersonation
# IMPORTANT: Headers are auto-generated to match this version
# Update this when Cloudflare starts blocking older versions
# Note: curl_cffi max supported is chrome136 as of v0.13.0
DEFAULT_CHROME_VERSION = "136"

# curl_cffi impersonate string (must match DEFAULT_CHROME_VERSION)
DEFAULT_IMPERSONATE = f"chrome{DEFAULT_CHROME_VERSION}"


def get_platform_headers(chrome_version: str = DEFAULT_CHROME_VERSION) -> dict[str, str]:
    """
    Generate platform-specific headers based on current OS.

    Args:
        chrome_version: Chrome version number (e.g., "143")

    Returns:
        Dict with platform-specific headers for sec-ch-ua, sec-ch-ua-platform, and user-agent
    """
    system = platform.system()

    if system == "Windows":
        ua_platform = '"Windows"'
        user_agent = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
        )
    elif system == "Darwin":  # macOS
        ua_platform = '"macOS"'
        user_agent = (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
        )
    else:  # Linux and others
        ua_platform = '"Linux"'
        user_agent = (
            f"Mozilla/5.0 (X11; Linux x86_64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36"
        )

    return {
        "sec-ch-ua": f'"Google Chrome";v="{chrome_version}", "Chromium";v="{chrome_version}", "Not A(Brand";v="24"',
        "sec-ch-ua-platform": ua_platform,
        "user-agent": user_agent,
    }


def load_config(config_path: Path | str | None = None) -> dict[str, Any]:
    """
    Load full configuration from config file.

    Args:
        config_path: Path to config file. Defaults to ~/.grok-config.json

    Returns:
        Dict with 'cookies' (GrokCookies) and 'headers' (dict, may be empty)

    Raises:
        GrokConfigError: If config file is missing or invalid
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise GrokConfigError(
            f"Config file not found: {config_path}\n\n"
            f"Please create {config_path} with your Grok cookies.\n"
            f"See README.md for instructions."
        )

    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise GrokConfigError(f"Invalid JSON in config file: {e}") from e

    if "cookies" not in config:
        raise GrokConfigError(
            "Config file missing 'cookies' key.\n"
            "Expected format:\n"
            '{\n  "cookies": {\n    "sso": "...",\n    "sso-rw": "...",\n'
            '    "x-userid": "...",\n    "cf_clearance": "..."\n  }\n}'
        )

    try:
        cookies = GrokCookies(**config["cookies"])
    except Exception as e:
        raise GrokConfigError(f"Invalid cookie configuration: {e}") from e

    # Get custom headers from config (optional)
    custom_headers = config.get("headers", {})

    # Get impersonate version from config (optional)
    # Can be overridden in config file: {"impersonate": "chrome142"}
    impersonate = config.get("impersonate", DEFAULT_IMPERSONATE)

    return {
        "cookies": cookies,
        "headers": custom_headers,
        "impersonate": impersonate,
    }


def load_cookies(config_path: Path | str | None = None) -> GrokCookies:
    """
    Load authentication cookies from config file.

    Args:
        config_path: Path to config file. Defaults to ~/.grok-config.json

    Returns:
        GrokCookies instance

    Raises:
        GrokConfigError: If config file is missing or invalid
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise GrokConfigError(
            f"Config file not found: {config_path}\n\n"
            f"Please create {config_path} with your Grok cookies.\n"
            f"See README.md for instructions."
        )

    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise GrokConfigError(f"Invalid JSON in config file: {e}") from e

    if "cookies" not in config:
        raise GrokConfigError(
            "Config file missing 'cookies' key.\n"
            "Expected format:\n"
            '{\n  "cookies": {\n    "sso": "...",\n    "sso-rw": "...",\n'
            '    "x-userid": "...",\n    "cf_clearance": "..."\n  }\n}'
        )

    try:
        return GrokCookies(**config["cookies"])
    except Exception as e:
        raise GrokConfigError(f"Invalid cookie configuration: {e}") from e


def save_cookies(cookies: GrokCookies, config_path: Path | str | None = None) -> None:
    """
    Save authentication cookies to config file.

    Args:
        cookies: GrokCookies instance to save
        config_path: Path to config file. Defaults to ~/.grok-config.json
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    else:
        config_path = Path(config_path)

    config = {"cookies": cookies.model_dump(by_alias=True)}

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
