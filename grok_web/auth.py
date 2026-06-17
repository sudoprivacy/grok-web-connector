"""Authentication and configuration management for Grok Web Connector."""

import json
from pathlib import Path
from typing import Any

from .exceptions import GrokConfigError
from .models import GrokCookies

DEFAULT_CONFIG_PATH = Path.home() / ".grok-config.json"


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

    return {
        "cookies": cookies,
        "headers": config.get("headers", {}),
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


def load_api_key(config_path: Path | str | None = None) -> str | None:
    """Load xAI API key from environment or config file.

    Resolution order: $XAI_API_KEY env var → "xai_api_key" in config file.

    Args:
        config_path: Path to config file. Defaults to ~/.grok-config.json

    Returns:
        API key string, or None if not configured.
    """
    import os

    key = os.environ.get("XAI_API_KEY")
    if key:
        return key

    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        return None

    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    return config.get("xai_api_key")


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
