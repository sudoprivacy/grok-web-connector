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

from ai_dev_browser import DEFAULT_DEBUG_PORT
from ai_dev_browser.core import browser_start
from ai_dev_browser.core.connection import connect_browser

from .auth import DEFAULT_CONFIG_PATH
from .client import GROK_CHROME_PROFILE

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
            # Launch Chrome via ai-dev-browser
            print("🌐 Launching Chrome...")
            result = browser_start(
                port=DEFAULT_DEBUG_PORT,
                headless=headless,
                profile=GROK_CHROME_PROFILE,
            )
            if "error" in result:
                raise RuntimeError(result["error"])
            actual_port = result["port"]

            # Connect to Chrome
            browser = await connect_browser(
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
            # Browser returns Cookie objects with direct attribute access
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

    def refresh_cookies(
        self,
        sso: str | None = None,
        sso_rw: str | None = None,
        userid: str | None = None,
        cf_clearance: str | None = None,
        interactive: bool = True,
    ) -> bool:
        """Refresh saved cookies without driving an automation browser.

        Useful when the automation Chrome is Turnstile-blocked and
        cannot complete the interactive ``setup`` flow. User opens
        their regular browser → signs in at grok.com → copies each
        cookie value from DevTools (Application → Cookies), and pastes
        them here.

        Any argument left as None (and ``interactive=True``) prompts
        the user; leaving the prompt blank keeps whatever's currently
        in the config. So the common "only cf_clearance expired" case
        is one non-blank paste + three empty enters.

        Sanity warnings (non-fatal):
        - ``sso`` / ``sso-rw`` usually look like JWTs (``a.b.c``).
        - ``x-userid`` is a UUID.
        - ``cf_clearance`` is usually >50 chars.

        Non-interactive mode (``interactive=False``) requires at least
        one non-None argument; any unset field retains its existing
        saved value (no prompting, no clearing).
        """
        import re

        existing: dict[str, str] = {}
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    existing = (json.load(f) or {}).get("cookies", {}) or {}
            except Exception:
                pass

        def _resolve(field: str, new: str | None, current: str) -> str:
            if new is not None:
                return new.strip()
            if not interactive:
                return current
            preview = (current[:20] + "…") if current else "<unset>"
            raw = input(f"  {field} [{preview}]: ").strip()
            return raw if raw else current

        # Resolve each field (prompt or keep).
        new_sso = _resolve("sso", sso, existing.get("sso", ""))
        new_sso_rw = _resolve("sso-rw", sso_rw, existing.get("sso-rw", ""))
        new_userid = _resolve("x-userid", userid, existing.get("x-userid", ""))
        new_cf = _resolve("cf_clearance", cf_clearance, existing.get("cf_clearance", ""))

        # Sanity checks (warn but don't fail — Grok formats evolve).
        if new_sso and new_sso.count(".") != 2:
            print(f"  ⚠ sso doesn't look like a JWT (expected a.b.c, got {len(new_sso)} chars)")
        if new_sso_rw and new_sso_rw.count(".") != 2:
            print(
                f"  ⚠ sso-rw doesn't look like a JWT (expected a.b.c, got {len(new_sso_rw)} chars)"
            )
        uuid_re = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
        )
        if new_userid and not uuid_re.match(new_userid):
            print(f"  ⚠ x-userid doesn't look like a UUID (got {len(new_userid)} chars)")
        if new_cf and len(new_cf) < 30:
            print(f"  ⚠ cf_clearance is shorter than expected ({len(new_cf)} chars, usually >50)")

        required = {
            "sso": new_sso,
            "sso-rw": new_sso_rw,
            "x-userid": new_userid,
            "cf_clearance": new_cf,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            print(f"❌ Missing required cookies: {missing}")
            return False

        self._save_cookies(
            {
                "sso": new_sso,
                "sso-rw": new_sso_rw,
                "x-userid": new_userid,
                "cf_clearance": new_cf,
            }
        )
        print(f"✅ Cookies written to: {self.config_path}")
        return True


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Manage Grok authentication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m grok_web.auth_manager setup             # Interactive login via automation browser
  python -m grok_web.auth_manager status            # Check auth status
  python -m grok_web.auth_manager clear             # Clear saved auth
  python -m grok_web.auth_manager refresh-cookies   # Paste cookies from your regular browser
  python -m grok_web.auth_manager refresh-cookies --cf-clearance "<value>"
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

    # refresh-cookies — paste cookies from a non-automation browser
    # (primary remedy when CF Turnstile flags the automation Chrome).
    refresh_parser = subparsers.add_parser(
        "refresh-cookies",
        help=(
            "Paste new cookie values without launching an automation "
            "browser (use when CF Turnstile blocks the setup flow)."
        ),
    )
    refresh_parser.add_argument(
        "--sso", default=None, help="New sso cookie value (JWT); omit to prompt / keep"
    )
    refresh_parser.add_argument(
        "--sso-rw", default=None, help="New sso-rw cookie value (JWT); omit to prompt / keep"
    )
    refresh_parser.add_argument(
        "--userid", default=None, help="New x-userid value (UUID); omit to prompt / keep"
    )
    refresh_parser.add_argument(
        "--cf-clearance",
        default=None,
        help="New cf_clearance value; omit to prompt / keep",
    )
    refresh_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Do not prompt for unset fields; keep their existing values. "
            "Useful in scripts that only refresh cf_clearance."
        ),
    )

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

    elif args.command == "refresh-cookies":
        print()
        print("🔐 Refresh Grok cookies (paste from your regular browser)")
        print("=" * 60)
        print("  In your normal Chrome/Firefox (NOT the automation one):")
        print("    1. Visit https://grok.com and sign in if needed")
        print("    2. DevTools → Application (or Storage) → Cookies → https://grok.com")
        print("    3. Copy each cookie's Value column")
        print()
        print("  Press ENTER at a prompt to keep the currently-saved value.")
        print()
        ok = auth.refresh_cookies(
            sso=args.sso,
            sso_rw=getattr(args, "sso_rw", None),
            userid=args.userid,
            cf_clearance=getattr(args, "cf_clearance", None),
            interactive=not args.non_interactive,
        )
        if not ok:
            exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
