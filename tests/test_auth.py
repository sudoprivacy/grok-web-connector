"""Tests for auth.py module."""

import json
import os
import tempfile

import pytest

from grok_web.auth import (
    DEFAULT_CHROME_VERSION,
    DEFAULT_IMPERSONATE,
    get_platform_headers,
    load_config,
    load_cookies,
    save_cookies,
)
from grok_web.exceptions import GrokConfigError
from grok_web.models import GrokCookies


class TestDefaultConstants:
    """Tests for default constants."""

    def test_default_chrome_version(self):
        """DEFAULT_CHROME_VERSION is set."""
        assert DEFAULT_CHROME_VERSION is not None
        assert isinstance(DEFAULT_CHROME_VERSION, str)

    def test_default_impersonate(self):
        """DEFAULT_IMPERSONATE matches chrome version."""
        assert DEFAULT_IMPERSONATE.startswith("chrome")
        assert DEFAULT_CHROME_VERSION in DEFAULT_IMPERSONATE


class TestGetPlatformHeaders:
    """Tests for get_platform_headers function."""

    def test_contains_required_headers(self):
        """Returns dict with required headers."""
        result = get_platform_headers()
        assert "user-agent" in result
        assert "sec-ch-ua" in result
        assert "sec-ch-ua-platform" in result
        assert "Chrome" in result["user-agent"]

    def test_custom_chrome_version(self):
        """Accepts custom chrome version."""
        result = get_platform_headers("142")
        assert "142" in result["user-agent"]
        assert "142" in result["sec-ch-ua"]

    def test_windows_platform(self):
        """Returns Windows-specific headers when on Windows."""
        from unittest.mock import patch

        with patch("grok_web.auth.platform.system", return_value="Windows"):
            result = get_platform_headers()
            assert "Windows" in result["user-agent"]
            assert '"Windows"' in result["sec-ch-ua-platform"]

    def test_linux_platform(self):
        """Returns Linux-specific headers when on Linux."""
        from unittest.mock import patch

        with patch("grok_web.auth.platform.system", return_value="Linux"):
            result = get_platform_headers()
            assert "Linux" in result["user-agent"]
            assert '"Linux"' in result["sec-ch-ua-platform"]


class TestLoadCookies:
    """Tests for load_cookies function."""

    def test_load_from_valid_file(self):
        """Load cookies from valid JSON file with nested structure."""
        config_data = {
            "cookies": {
                "sso": "test_sso",
                "sso-rw": "test_sso_rw",
                "x-userid": "test_userid",
                "cf_clearance": "test_cf",
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name

        try:
            cookies = load_cookies(temp_path)
            assert isinstance(cookies, GrokCookies)
            assert cookies.sso == "test_sso"
            assert cookies.sso_rw == "test_sso_rw"
            assert cookies.x_userid == "test_userid"
            assert cookies.cf_clearance == "test_cf"
        finally:
            os.unlink(temp_path)

    def test_load_from_nonexistent_file(self):
        """Raise error when file doesn't exist."""
        with pytest.raises(GrokConfigError, match="not found"):
            load_cookies("/nonexistent/path/cookies.json")

    def test_load_from_invalid_json(self):
        """Raise error when file contains invalid JSON."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            temp_path = f.name

        try:
            with pytest.raises(GrokConfigError, match="Invalid JSON"):
                load_cookies(temp_path)
        finally:
            os.unlink(temp_path)

    def test_load_missing_cookies_key(self):
        """Raise error when 'cookies' key is missing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"sso": "test"}, f)  # Missing 'cookies' wrapper
            temp_path = f.name

        try:
            with pytest.raises(GrokConfigError, match="missing 'cookies' key"):
                load_cookies(temp_path)
        finally:
            os.unlink(temp_path)

    def test_load_missing_required_fields(self):
        """Raise error when required cookie fields are missing."""
        config_data = {
            "cookies": {
                "sso": "test_sso",
                # Missing other required fields
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name

        try:
            with pytest.raises(GrokConfigError, match="Invalid cookie"):
                load_cookies(temp_path)
        finally:
            os.unlink(temp_path)


class TestSaveCookies:
    """Tests for save_cookies function."""

    def test_save_cookies(self):
        """Save cookies to file in nested format."""
        cookies = GrokCookies(
            sso="test_sso",
            **{"sso-rw": "test_sso_rw"},
            **{"x-userid": "test_userid"},
            cf_clearance="test_cf",
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            temp_path = f.name

        try:
            save_cookies(cookies, temp_path)

            # Read back and verify
            with open(temp_path) as f:
                saved_data = json.load(f)

            # Should have nested 'cookies' key
            assert "cookies" in saved_data
            assert saved_data["cookies"]["sso"] == "test_sso"
            assert saved_data["cookies"]["sso-rw"] == "test_sso_rw"
            assert saved_data["cookies"]["x-userid"] == "test_userid"
            assert saved_data["cookies"]["cf_clearance"] == "test_cf"
        finally:
            os.unlink(temp_path)


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_from_valid_config(self):
        """Load full config from valid file."""
        config_data = {
            "cookies": {
                "sso": "test_sso",
                "sso-rw": "test_sso_rw",
                "x-userid": "test_userid",
                "cf_clearance": "test_cf",
            },
            "impersonate": "chrome140",
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name

        try:
            config = load_config(temp_path)

            assert "cookies" in config
            assert "impersonate" in config
            assert config["impersonate"] == "chrome140"
            assert isinstance(config["cookies"], GrokCookies)
        finally:
            os.unlink(temp_path)

    def test_load_uses_default_impersonate(self):
        """Uses default impersonate when not in config."""
        config_data = {
            "cookies": {
                "sso": "test_sso",
                "sso-rw": "test_sso_rw",
                "x-userid": "test_userid",
                "cf_clearance": "test_cf",
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name

        try:
            config = load_config(temp_path)
            assert config["impersonate"] == DEFAULT_IMPERSONATE
        finally:
            os.unlink(temp_path)

    def test_load_includes_headers(self):
        """Includes custom headers if present."""
        config_data = {
            "cookies": {
                "sso": "test_sso",
                "sso-rw": "test_sso_rw",
                "x-userid": "test_userid",
                "cf_clearance": "test_cf",
            },
            "headers": {"X-Custom": "value"},
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name

        try:
            config = load_config(temp_path)
            assert config["headers"] == {"X-Custom": "value"}
        finally:
            os.unlink(temp_path)

    def test_load_nonexistent_config_raises(self):
        """Raise error when config file doesn't exist."""
        with pytest.raises(GrokConfigError, match="not found"):
            load_config("/nonexistent/path/config.json")

    def test_load_missing_cookies_key_raises(self):
        """Raise error when 'cookies' key is missing."""
        config_data = {"impersonate": "chrome140"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name

        try:
            with pytest.raises(GrokConfigError, match="missing 'cookies' key"):
                load_config(temp_path)
        finally:
            os.unlink(temp_path)

    def test_load_invalid_json_raises(self):
        """Raise error when config file has invalid JSON."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            temp_path = f.name

        try:
            with pytest.raises(GrokConfigError, match="Invalid JSON"):
                load_config(temp_path)
        finally:
            os.unlink(temp_path)

    def test_load_invalid_cookie_config_raises(self):
        """Raise error when cookie fields are invalid."""
        config_data = {
            "cookies": {
                "sso": "test_sso",
                # Missing required fields
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            temp_path = f.name

        try:
            with pytest.raises(GrokConfigError, match="Invalid cookie"):
                load_config(temp_path)
        finally:
            os.unlink(temp_path)
