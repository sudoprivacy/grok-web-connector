#!/usr/bin/env python3
"""
Authentication Manager for Grok Web Connector.

Provides interactive login flow to automatically extract and save cookies.
Eliminates the need to manually copy cookies from browser DevTools.

Usage:
    python -m grok_web.auth_manager setup     # Interactive login
    python -m grok_web.auth_manager status    # Check auth status
    python -m grok_web.auth_manager clear     # Clear saved auth
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import nodriver as uc

from .auth import DEFAULT_CONFIG_PATH
from .browser import DEFAULT_DEBUG_PORT, ensure_chrome_running

# Cookies we need from Grok
REQUIRED_COOKIES = {"sso", "sso-rw", "x-userid", "cf_clearance"}


class AuthManager:
    """Manages authentication for Grok Web Connector."""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or DEFAULT_CONFIG_PATH

    def is_authenticated(self) -> bool:
        """Check if valid authentication exists."""
        if not self.config_path.exists():
            return False

        try:
            with open(self.config_path) as f:
                config = json.load(f)

            cookies = config.get("cookies", {})
            # Check all required cookies exist
            return all(cookies.get(name) for name in REQUIRED_COOKIES)
        except (json.JSONDecodeError, KeyError):
            return False

    def get_auth_info(self) -> dict:
        """Get authentication status info."""
        info = {
            "authenticated": self.is_authenticated(),
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
        }

        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    config = json.load(f)
                cookies = config.get("cookies", {})
                info["has_sso"] = bool(cookies.get("sso"))
                info["has_sso_rw"] = bool(cookies.get("sso-rw"))
                info["has_userid"] = bool(cookies.get("x-userid"))
                info["has_cf_clearance"] = bool(cookies.get("cf_clearance"))

                # Check file age
                age_hours = (time.time() - self.config_path.stat().st_mtime) / 3600
                info["config_age_hours"] = round(age_hours, 1)
            except Exception:
                pass

        return info

    async def setup_auth(self, timeout_minutes: int = 5, headless: bool = False) -> bool:
        """
        Interactive authentication setup.

        Launches Chrome, navigates to Grok, waits for user login,
        then extracts and saves cookies.

        Args:
            timeout_minutes: Max time to wait for login
            headless: Run headless (not recommended for login)

        Returns:
            True if authentication successful
        """
        print("🔐 Starting Grok authentication setup...")
        print(f"   Config will be saved to: {self.config_path}")
        print(f"   Timeout: {timeout_minutes} minutes")
        print()

        browser = None
        try:
            # Launch Chrome (ensure_chrome_running returns tuple of (process, actual_port))
            print("🌐 Launching Chrome...")
            _, actual_port = await ensure_chrome_running(
                port=DEFAULT_DEBUG_PORT,
                headless=headless,
            )

            # Connect with nodriver to the running Chrome
            browser = await uc.start(
                host="127.0.0.1",
                port=actual_port,
            )

            # Navigate to Grok
            print("📍 Navigating to grok.com...")
            await browser.get("https://grok.com")

            # Wait for Cloudflare if needed
            await asyncio.sleep(2)

            # Check if already logged in
            cookies = await self._get_cookies(browser)
            if self._has_required_cookies(cookies):
                print("✅ Already logged in! Extracting cookies...")
                self._save_cookies(cookies)
                print(f"💾 Cookies saved to: {self.config_path}")
                return True

            # Wait for user to login
            print()
            print("=" * 50)
            print("👤 Please log in to your Grok account in the browser window.")
            print("   The script will automatically detect when you're logged in.")
            print("=" * 50)
            print()

            timeout_seconds = timeout_minutes * 60
            start_time = time.time()
            check_interval = 2  # seconds

            while time.time() - start_time < timeout_seconds:
                elapsed = int(time.time() - start_time)
                remaining = timeout_seconds - elapsed

                # Check cookies
                cookies = await self._get_cookies(browser)
                if self._has_required_cookies(cookies):
                    print()
                    print("✅ Login detected! Extracting cookies...")
                    self._save_cookies(cookies)
                    print(f"💾 Cookies saved to: {self.config_path}")
                    return True

                # Progress update
                if elapsed % 10 == 0:
                    print(f"⏳ Waiting for login... ({remaining}s remaining)")

                await asyncio.sleep(check_interval)

            print()
            print(f"❌ Timeout after {timeout_minutes} minutes. Please try again.")
            return False

        except Exception as e:
            print(f"❌ Error during setup: {e}")
            return False

        finally:
            if browser:
                import contextlib

                with contextlib.suppress(Exception):
                    browser.stop()

    async def _get_cookies(self, browser) -> dict[str, str]:
        """Extract cookies from browser."""
        cookies = {}
        try:
            # Get all cookies for grok.com
            # nodriver returns Cookie objects with direct attribute access
            all_cookies = await browser.cookies.get_all()
            for cookie in all_cookies:
                domain = getattr(cookie, "domain", "")
                name = getattr(cookie, "name", "")
                value = getattr(cookie, "value", "")

                if ("grok.com" in domain or "x.ai" in domain) and name in REQUIRED_COOKIES:
                    cookies[name] = value
        except Exception as e:
            print(f"⚠️ Error getting cookies: {e}")
            import traceback

            traceback.print_exc()
        return cookies

    def _has_required_cookies(self, cookies: dict[str, str]) -> bool:
        """Check if all required cookies are present."""
        return all(cookies.get(name) for name in REQUIRED_COOKIES)

    def _save_cookies(self, cookies: dict[str, str]) -> None:
        """Save cookies to config file."""
        # Load existing config or create new
        config = {}
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    config = json.load(f)
            except Exception:
                pass

        # Update cookies
        config["cookies"] = {
            "sso": cookies.get("sso", ""),
            "sso-rw": cookies.get("sso-rw", ""),
            "x-userid": cookies.get("x-userid", ""),
            "cf_clearance": cookies.get("cf_clearance", ""),
        }

        # Add metadata
        config["_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        config["_updated_by"] = "grok_web.auth_manager"

        # Save
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(config, f, indent=2)

    def clear_auth(self) -> bool:
        """Clear saved authentication."""
        if self.config_path.exists():
            self.config_path.unlink()
            print(f"🗑️ Removed: {self.config_path}")
            return True
        else:
            print("ℹ️ No config file to remove")
            return False


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Manage Grok authentication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m grok_web.auth_manager setup     # Interactive login
  python -m grok_web.auth_manager status    # Check auth status
  python -m grok_web.auth_manager clear     # Clear saved auth
""",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # setup
    setup_parser = subparsers.add_parser("setup", help="Interactive login setup")
    setup_parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        help="Login timeout in minutes (default: 5)",
    )
    setup_parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless (not recommended)",
    )

    # status
    subparsers.add_parser("status", help="Check authentication status")

    # clear
    subparsers.add_parser("clear", help="Clear saved authentication")

    args = parser.parse_args()

    auth = AuthManager()

    if args.command == "setup":
        success = asyncio.run(
            auth.setup_auth(
                timeout_minutes=args.timeout,
                headless=args.headless,
            )
        )
        if success:
            print()
            print("✅ Authentication setup complete!")
            print("   You can now use grok-web-connector.")
        else:
            print()
            print("❌ Authentication setup failed.")
            exit(1)

    elif args.command == "status":
        info = auth.get_auth_info()
        print()
        print("🔐 Grok Authentication Status")
        print("=" * 40)
        print(f"   Config file: {info['config_path']}")
        print(f"   Exists: {'Yes' if info['config_exists'] else 'No'}")
        print(f"   Authenticated: {'Yes' if info['authenticated'] else 'No'}")

        if info.get("config_exists"):
            print()
            print("   Cookies:")
            print(f"     sso:          {'✅' if info.get('has_sso') else '❌'}")
            print(f"     sso-rw:       {'✅' if info.get('has_sso_rw') else '❌'}")
            print(f"     x-userid:     {'✅' if info.get('has_userid') else '❌'}")
            print(f"     cf_clearance: {'✅' if info.get('has_cf_clearance') else '❌'}")

            if info.get("config_age_hours"):
                print()
                print(f"   Config age: {info['config_age_hours']} hours")

    elif args.command == "clear":
        auth.clear_auth()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
