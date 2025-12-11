"""
Refresh cf_clearance Cookie for Grok API

This utility opens grok.com in your real Chrome browser to refresh the
cf_clearance cookie required for API access. The cookie binds to your
browser's TLS fingerprint.

Usage:
    python -m grok_web.refresh_cf_clearance

Requirements:
    - playwright installed: pip install grok-web-connector[playwright]
    - Playwright Chromium: playwright install chromium
    - Chrome browser installed (for best TLS fingerprint match)
    - ~/.grok-config.json with existing cookies

The script will:
1. Open grok.com in Chrome with existing cookies
2. Wait for any Cloudflare challenge to complete
3. Test the API to verify the cookie works
4. Save the new cf_clearance to ~/.grok-config.json
"""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def refresh_cf_clearance():
    """Open browser to refresh cf_clearance cookie."""
    config_path = Path.home() / ".grok-config.json"

    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        print("Please create it with your Grok cookies first.")
        return False

    with open(config_path) as f:
        config = json.load(f)

    cookies = config.get("cookies", {})
    if not cookies:
        print("Error: No cookies found in config file")
        return False

    async with async_playwright() as p:
        # Launch Playwright Chromium (version closer to curl_cffi's chrome136)
        # Using system Chrome causes TLS fingerprint mismatch with curl_cffi
        print("Launching Playwright Chromium...")
        browser = await p.chromium.launch(
            headless=False,
            # Removed channel="chrome" to use Playwright's bundled Chromium
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )

        # Add existing cookies
        cookie_list = []
        for name in ["sso", "sso-rw", "x-userid", "cf_clearance"]:
            if name in cookies:
                cookie_list.append(
                    {
                        "name": name,
                        "value": cookies[name],
                        "domain": ".grok.com",
                        "path": "/",
                    }
                )
        await context.add_cookies(cookie_list)

        page = await context.new_page()

        print("\nOpening grok.com...")
        print("If Cloudflare shows a challenge, please complete it.")
        print("Waiting up to 90 seconds...\n")

        try:
            await page.goto("https://grok.com/imagine", timeout=90000, wait_until="load")
        except Exception as e:
            print(f"Navigation: {e}")

        # Wait for page to settle
        await asyncio.sleep(5)
        print(f"Current URL: {page.url}")

        # Test API via browser fetch
        print("\nTesting API...")
        result = await page.evaluate("""
            async () => {
                try {
                    const resp = await fetch("/rest/media/post/list", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({limit: 1, filter: {}})
                    });
                    return {status: resp.status, ok: resp.ok};
                } catch(e) {
                    return {error: e.toString()};
                }
            }
        """)

        if result.get("status") == 200:
            print("API works!")

            # Get updated cookies
            all_cookies = await context.cookies()
            new_cf = None
            for c in all_cookies:
                if c["name"] == "cf_clearance":
                    new_cf = c["value"]
                    break

            if new_cf and new_cf != cookies.get("cf_clearance"):
                print("\nSaving new cf_clearance to config...")
                config["cookies"]["cf_clearance"] = new_cf
                with open(config_path, "w") as f:
                    json.dump(config, f, indent=2)
                print("Config updated!")
            else:
                print("\ncf_clearance unchanged.")

            await asyncio.sleep(2)
            await browser.close()
            return True
        else:
            print(f"\nAPI failed: {result}")
            print("\nPlease complete the Cloudflare challenge in the browser.")
            print("Waiting 30 more seconds...")
            await asyncio.sleep(30)

            # Retry
            result = await page.evaluate("""
                async () => {
                    try {
                        const resp = await fetch("/rest/media/post/list", {
                            method: "POST",
                            headers: {"Content-Type": "application/json"},
                            body: JSON.stringify({limit: 1, filter: {}})
                        });
                        return {status: resp.status, ok: resp.ok};
                    } catch(e) {
                        return {error: e.toString()};
                    }
                }
            """)

            if result.get("status") == 200:
                all_cookies = await context.cookies()
                for c in all_cookies:
                    if c["name"] == "cf_clearance":
                        config["cookies"]["cf_clearance"] = c["value"]
                        with open(config_path, "w") as f:
                            json.dump(config, f, indent=2)
                        print("\nSuccess! Config updated.")
                        await browser.close()
                        return True

            print("\nFailed to refresh cf_clearance.")
            await browser.close()
            return False


def main():
    """Entry point for running as module or script."""
    success = asyncio.run(refresh_cf_clearance())
    return 0 if success else 1


if __name__ == "__main__":
    exit(main())
