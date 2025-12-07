"""Authentication and configuration management for Grok Web Connector."""

import json
from pathlib import Path

from .exceptions import GrokConfigError
from .models import GrokCookies


DEFAULT_CONFIG_PATH = Path.home() / ".grok-config.json"


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
        raise GrokConfigError(f"Invalid JSON in config file: {e}")

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
        raise GrokConfigError(f"Invalid cookie configuration: {e}")


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
