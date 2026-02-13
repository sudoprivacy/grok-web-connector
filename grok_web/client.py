"""
Grok Web Connector - API Clients

NodriverClient
    - Browser automation client using nodriver/CDP
    - Handles all Grok API operations (reads, writes, video generation)

SmartGrokClient
    - Thin wrapper around NodriverClient with cookie loading and interactive login

Public API:
    Use get_client() from grok_web package - returns SmartGrokClient
    which automatically routes to NodriverClient.
"""

import logging
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, overload

from ._internal import (
    MEDIA_POST_LIKE_ENDPOINT,
    AsyncClientBase,
    build_video_payload,
    generate_statsig_id,
    parse_video_ndjson_response,
    resolve_preset,
)
from .auth import DEFAULT_CONFIG_PATH, load_config, save_cookies
from .browser import DEFAULT_DEBUG_HOST
from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from .models import (
    GrokCookies,
    ImageEditResult,
    ImageGenerationResult,
    VideoGenerationResult,
    VideoPreset,
)

# =============================================================================
# Logging
# =============================================================================

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

# x-statsig-id is required for chat API (create_video_from_image)
# This appears to be a Statsig SDK client ID, reusable across requests
DEFAULT_STATSIG_ID = (
    "W6IFgVSv2YSVxFj5Yt971KvAL1ldD75XJoGIR285iLdGPIiPNM7S1C9An8vmKsYb"
    "R9N5sF963w2iXoRhwSHYizPczaEUWA"
)


# =============================================================================
# NodriverClient - Stealth browser using nodriver
# =============================================================================


class NodriverClient(AsyncClientBase):
    """
    Stealth browser client using nodriver/CDP.

    Uses Chrome DevTools Protocol without WebDriver traces.
    Automatically handles Cloudflare Turnstile.

    Features:
        - Auto-launches isolated Chrome instance if not running
        - Reuses existing Chrome session for fast subsequent calls
        - Handles Cloudflare challenges automatically

    Usage:
        >>> # Simplest usage - auto-launches Chrome if needed
        >>> async with NodriverClient() as client:
        ...     posts = await client.list_posts(limit=10)
        ...     result = await client.create_video_via_ui(post_id)

        >>> # Or use get_client() factory
        >>> from grok_web import get_client
        >>> async with get_client() as client:
        ...     posts = await client.list_posts()

        >>> # Connect to specific Chrome instance
        >>> async with NodriverClient(port=9223) as client:
        ...     posts = await client.list_posts()

    Performance:
        - First run: ~5s (launches Chrome, handles Cloudflare)
        - Subsequent runs: instant (reuses browser session)
        - Chrome stays open between script runs for fast batch processing
    """

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
        headless: bool = False,
        host: str | None = None,
        port: int | None = None,
        auto_launch: bool = True,
        ui_delay: float = 1.0,
        force_new_chrome: bool = False,
        profile: str | None = None,
    ):
        """
        Initialize NodriverClient.

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Defaults to ~/.grok-config.json
            headless: Run browser in headless mode (default: False for debugging)
            host: Remote debugging host. Defaults to "127.0.0.1".
            port: Remote debugging port. None = auto-assigned by ai-dev-browser.
            auto_launch: If True (default), automatically launch Chrome if not running.
                        Set to False to only connect to existing Chrome.
            ui_delay: Multiplier for UI operation delays (default: 1.0).
                     Increase for slower connections, decrease for faster ones.
            force_new_chrome: If True, always launch new Chrome (skip reuse logic).
                     Use this in BrowserWorkerPool to avoid race conditions.
            profile: Chrome profile name for start_browser (default: "grok-chrome").
                     Worker pool uses per-worker profiles like "grok-chrome-w0".
        """
        super().__init__()  # Initialize business logic layer

        # Store config path for cookie auto-save
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]

        self.cookies = cookies
        self._headless = headless
        self._browser = None
        self._tab = None
        self._initialized = False
        self._chrome_process = None  # Track Chrome process we launched
        self._ui_delay = ui_delay

        # Browser connection settings
        self._remote_host = host or DEFAULT_DEBUG_HOST
        self._remote_port = port  # None = let start_browser auto-assign
        self._auto_launch = auto_launch
        self._force_new_chrome = force_new_chrome
        self._profile = profile

    async def __aenter__(self):
        import asyncio

        from ai_dev_browser.core.connection import connect_browser
        from nodriver import cdp

        from .browser import ensure_chrome_running

        # Ensure Chrome is running (auto-launch if needed)
        actual_port = self._remote_port  # Default to requested port
        if self._auto_launch:
            try:
                self._chrome_process, actual_port = await ensure_chrome_running(
                    host=self._remote_host,
                    port=self._remote_port,
                    headless=self._headless,
                    force_new=self._force_new_chrome,
                    profile=self._profile,
                )
            except FileNotFoundError as e:
                raise GrokAPIError(str(e)) from e
            except (TimeoutError, RuntimeError) as e:
                raise GrokAPIError(f"Chrome failed to start: {e}") from e

        # Store actual port (may differ from requested if auto-assigned)
        self._remote_port = actual_port

        # Connect to Chrome via ai-dev-browser (connects to existing instance)
        try:
            self._browser = await connect_browser(
                host=self._remote_host,
                port=actual_port,
            )
        except Exception as e:
            raise GrokAPIError(
                f"Failed to connect to Chrome at {self._remote_host}:{actual_port}: {e}"
            ) from e

        # Try to reuse existing grok.com tab, or use first available page tab
        self._tab = None
        try:
            targets = getattr(self._browser, "targets", None) or []
            # Filter for page targets only (not iframes, background_pages, etc.)
            page_targets = [t for t in targets if getattr(t, "type_", "") == "page"]
            for target in page_targets:
                url = getattr(target, "url", "") or ""
                if "grok.com" in url:
                    self._tab = target
                    break
            if self._tab is None and page_targets:
                self._tab = page_targets[0]
        except Exception:
            pass  # Fall through to create new tab

        if self._tab is None:
            self._tab = await self._browser.get("about:blank")

        # Set cookies via CDP before navigating to grok.com
        cookie_dict = self.cookies.to_dict()
        for name, value in cookie_dict.items():
            await self._tab.send(
                cdp.network.set_cookie(
                    name=name,
                    value=value,
                    domain=".grok.com",
                    path="/",
                    secure=True,
                    http_only=(name != "x-userid"),
                )
            )

        # Now navigate to grok.com with cookies already set
        await self._tab.get(f"{self.BASE_URL}/imagine")
        await asyncio.sleep(2)

        # Handle Cloudflare challenge if present
        from .nodriver_cf_verify import CFVerify

        cf_verify = CFVerify(_browser_tab=self._tab, _debug=True)
        success = await cf_verify.verify(_max_retries=15, _interval_between_retries=1)

        if not success:
            raise GrokAuthError("Failed to bypass Cloudflare challenge")

        self._initialized = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Don't stop Chrome - keep it running for reuse by subsequent calls
        # The Chrome process stays open in background, which is the desired behavior
        # for fast batch processing

        # Auto-save cookies on successful exit (no exception)
        # Use timeout to avoid hanging if Chrome was already killed
        if exc_type is None and self._initialized and self._browser:
            try:
                import asyncio

                await asyncio.wait_for(self._auto_save_cookies(), timeout=5.0)
            except Exception:
                pass  # Ignore errors (Chrome may already be dead)

        # Disconnect from tab to release attached state and allow Chrome reuse
        # This properly detaches from the page target, making is_chrome_in_use() return False
        if self._tab:
            try:
                import asyncio

                await asyncio.wait_for(self._tab.disconnect(), timeout=5.0)
            except Exception:
                pass  # Ignore disconnect errors (including timeout)

    async def _auto_save_cookies(self) -> None:
        """Extract cookies from browser and save to config file."""
        try:
            all_cookies = await self._browser.cookies.get_all()

            # Extract the cookies we need
            cookie_dict = {}
            required = {"sso", "sso-rw", "x-userid", "cf_clearance"}

            for cookie in all_cookies:
                domain = getattr(cookie, "domain", "")
                name = getattr(cookie, "name", "")
                value = getattr(cookie, "value", "")

                if ("grok.com" in domain or "x.ai" in domain) and name in required:
                    cookie_dict[name] = value

            # Only save if we got all required cookies
            if all(cookie_dict.get(name) for name in required):
                fresh_cookies = GrokCookies(**cookie_dict)
                save_cookies(fresh_cookies, self._config_path)
                logging.debug(f"Auto-saved cookies to {self._config_path}")

        except Exception as e:
            # Don't fail the operation if cookie save fails
            logging.debug(f"Failed to auto-save cookies: {e}")

    async def _evaluate_with_recovery(self, js_code: str, **kwargs) -> str:
        """Evaluate JS with auto-recovery on ExceptionDetails.

        When Chrome returns ExceptionDetails (execution context destroyed),
        reloads the page and retries once.
        """
        import asyncio

        result = await self._tab.evaluate(js_code, **kwargs)
        if isinstance(result, str):
            return result

        # ExceptionDetails - execution context is dead, recover
        logger.warning(
            f"ExceptionDetails from tab.evaluate(), recovering browser state "
            f"(got {type(result).__name__})"
        )
        await self._tab.get(f"{self.BASE_URL}/imagine")
        await asyncio.sleep(2)

        result = await self._tab.evaluate(js_code, **kwargs)
        if not isinstance(result, str):
            raise GrokAPIError(
                f"Browser evaluation failed after recovery. " f"Received: {type(result).__name__}."
            )
        return result

    async def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request via browser fetch.

        Includes required headers that Grok expects:
        - x-xai-request-id: UUID for request tracking
        - x-statsig-id: for feature flags (from localStorage)
        """
        import json as json_module
        import uuid

        url = f"{self.BASE_URL}{endpoint}"
        payload_str = json_module.dumps(json_data) if json_data else "null"

        # Generate request ID like the browser does
        request_id = str(uuid.uuid4())

        # Get statsig ID from localStorage, fallback to default
        statsig_id = await self._tab.evaluate("""
            (() => {
                var keys = ['STATSIG_LOCAL_STORAGE_STABLE_ID', 'statsig_stable_id'];
                for (var key of keys) {
                    var val = localStorage.getItem(key);
                    if (val) return val;
                }
                return '';
            })()
        """)
        if not statsig_id:
            statsig_id = DEFAULT_STATSIG_ID

        # Escape the payload for embedding in JS string
        payload_escaped = payload_str.replace("\\", "\\\\").replace("'", "\\'")

        # Build headers matching browser behavior
        headers_js = f"""{{
            "Content-Type": "application/json",
            "x-xai-request-id": "{request_id}",
            "x-statsig-id": "{statsig_id}"
        }}"""

        js_code = f"""
        (async () => {{
            const resp = await fetch("{url}", {{
                method: "{method.upper()}",
                headers: {headers_js},
                body: '{payload_escaped}',
                credentials: "include"
            }});
            const text = await resp.text();
            return JSON.stringify({{status: resp.status, body: text}});
        }})()
        """

        result_str = await self._evaluate_with_recovery(
            js_code, await_promise=True, return_by_value=True
        )

        result = json_module.loads(result_str)

        if result["status"] in (401, 403):
            if "Just a moment" in result["body"]:
                raise GrokAuthError("Cloudflare challenge detected in API response")
            raise GrokAuthError(f"Request blocked ({result['status']})")

        if result["status"] == 404:
            raise GrokNotFoundError("Resource not found")

        if result["status"] >= 400:
            raise GrokAPIError(f"API error: {result['status']}")

        try:
            return json_module.loads(result["body"])
        except ValueError:
            return {}

    async def _api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Make authenticated request and return raw text response.

        Includes all headers that the browser sends when clicking UI buttons:
        - x-xai-request-id: UUID for request tracking
        - x-statsig-id: for feature flags (passed in extra_headers)
        - Referer: current page URL
        - baggage, sentry-trace, traceparent: telemetry headers
        """
        import json as json_module
        import uuid

        url = f"{self.BASE_URL}{endpoint}"
        payload_str = json_module.dumps(json_data) if json_data else "null"

        # Generate request ID like the browser does
        request_id = str(uuid.uuid4())

        # Generate sentry trace IDs (32 char hex)
        sentry_trace_id = uuid.uuid4().hex
        sentry_span_id = uuid.uuid4().hex[:16]
        traceparent_trace_id = uuid.uuid4().hex
        traceparent_span_id = uuid.uuid4().hex[:16]

        # Build headers matching browser behavior
        headers_obj = {
            "Content-Type": "application/json",
            "x-xai-request-id": request_id,
            "Referer": f"{self.BASE_URL}/imagine",
            "baggage": (
                "sentry-environment=production,"
                "sentry-release=bc5d5045c5beaefc9dfb4ec6d88d20247e4835ab,"
                "sentry-public_key=b311e0f2690c81f25e2c4cf6d4f7ce1c,"
                f"sentry-trace_id={sentry_trace_id},"
                "sentry-org_id=4508179396558848,"
                "sentry-sampled=false,"
                "sentry-sample_rand=0.01,"
                "sentry-sample_rate=0"
            ),
            "sentry-trace": f"{sentry_trace_id}-{sentry_span_id}-0",
            "traceparent": f"00-{traceparent_trace_id}-{traceparent_span_id}-00",
            **(extra_headers or {}),
        }
        headers_str = json_module.dumps(headers_obj)

        # Escape the payload for embedding in JS string
        payload_escaped = payload_str.replace("\\", "\\\\").replace("'", "\\'")

        js_code = f"""
        (async () => {{
            const resp = await fetch("{url}", {{
                method: "{method.upper()}",
                headers: {headers_str},
                body: '{payload_escaped}',
                credentials: "include"
            }});
            const text = await resp.text();
            return JSON.stringify({{status: resp.status, body: text}});
        }})()
        """

        result_str = await self._evaluate_with_recovery(
            js_code, await_promise=True, return_by_value=True
        )

        result = json_module.loads(result_str)

        if result["status"] in (401, 403):
            if "Just a moment" in result["body"]:
                raise GrokAuthError("Cloudflare challenge detected in API response")
            raise GrokAuthError(f"Request blocked ({result['status']})")

        if result["status"] == 404:
            raise GrokNotFoundError("Resource not found")

        if result["status"] >= 400:
            raise GrokAPIError(f"API error: {result['status']}")

        return result["body"]

    async def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL via CDP Network.loadNetworkResource.

        This bypasses JavaScript fetch CORS restrictions by using Chrome DevTools Protocol directly.
        Works reliably on both Windows and macOS.
        """
        from nodriver import cdp

        try:
            # Get the main frame ID for the current page
            frame_tree = await self._tab.send(cdp.page.get_frame_tree())
            frame_id = (
                frame_tree.frame.id_
            )  # Note: id_ (with underscore) because 'id' is a Python keyword

            # Use CDP Network.loadNetworkResource to fetch headers
            # This bypasses CORS and is more reliable than fetch()
            response = await self._tab.send(
                cdp.network.load_network_resource(
                    frame_id=frame_id,
                    url=asset_url,
                    options=cdp.network.LoadNetworkResourceOptions(
                        disable_cache=False, include_credentials=True
                    ),
                )
            )

            # Check if request succeeded
            if not response:
                raise GrokAPIError(f"Failed to load network resource: {asset_url}")

            # Response is directly LoadNetworkResourcePageResult (not wrapped)
            # Check if there was a network error
            if not response.success:
                error_msg = f"Network request failed for {asset_url}"
                if response.net_error_name:
                    error_msg += f": {response.net_error_name}"
                raise GrokAPIError(error_msg)

            # Check HTTP status
            if response.http_status_code == 403:
                raise GrokAuthError("Asset access denied (403)")
            if response.http_status_code and response.http_status_code >= 400:
                raise GrokAPIError(f"Asset request failed: HTTP {response.http_status_code}")

            # Get Content-Length from headers
            if response.headers:
                for header_name, header_value in response.headers.items():
                    if header_name.lower() == "content-length":
                        return int(header_value)

            raise GrokAPIError("No Content-Length header in response")

        except (GrokAPIError, GrokAuthError):
            raise
        except Exception as e:
            raise GrokAPIError(
                f"CDP network request failed for asset. " f"URL: {asset_url}, Error: {e}"
            ) from e

    # =========================================================================
    # NodriverClient-specific methods
    # =========================================================================

    async def _get_statsig_id_from_page(self) -> str | None:
        """Try to extract statsig_id from page context (localStorage, cookie, or JS var)."""
        # Try multiple locations where statsig_id might be stored
        js_code = """
        (function() {
            // Try localStorage (Statsig SDK typically stores here)
            var keys = ['STATSIG_LOCAL_STORAGE_STABLE_ID', 'statsig_stable_id', 'statsig_id', 'x-statsig-id'];
            for (var i = 0; i < keys.length; i++) {
                var val = localStorage.getItem(keys[i]);
                if (val) return val;
            }

            // Try sessionStorage
            for (var i = 0; i < keys.length; i++) {
                var val = sessionStorage.getItem(keys[i]);
                if (val) return val;
            }

            // Try window object
            if (window.statsigId) return window.statsigId;
            if (window.__STATSIG__) {
                try {
                    return window.__STATSIG__.stableID || window.__STATSIG__.userID;
                } catch(e) {}
            }

            return null;
        })()
        """
        try:
            result = await self._tab.evaluate(js_code, await_promise=False, return_by_value=True)
            if result:
                return str(result)
        except Exception:
            pass
        return None

    @staticmethod
    def generate_stable_id() -> str:
        """Generate a valid Statsig stable_id.

        Format: base64(70 random bytes) with padding stripped.
        This matches the format used by Statsig SDK.

        Returns:
            A 94-character base64-encoded string.

        Example:
            >>> stable_id = NodriverClient.generate_stable_id()
            >>> len(stable_id)
            94
        """
        import base64
        import os

        return base64.b64encode(os.urandom(70)).decode().rstrip("=")

    async def get_stable_id(self) -> str | None:
        """Get the current stable_id from localStorage.

        Returns:
            The stable_id string, or None if not set.
        """
        js_code = "localStorage.getItem('STATSIG_LOCAL_STORAGE_STABLE_ID')"
        try:
            result = await self._tab.evaluate(js_code, await_promise=False)
            return str(result) if result else None
        except Exception:
            return None

    async def set_stable_id(self, stable_id: str, reload_page: bool = True) -> bool:
        """Inject a custom stable_id into localStorage.

        This allows controlling the A/B testing bucket for video generation styles.
        The stable_id determines which style bucket you're assigned to.

        Args:
            stable_id: The stable_id to inject (use generate_stable_id() to create one)
            reload_page: Whether to reload the page after injection (default: True).
                        Set to False if you'll navigate elsewhere immediately.

        Returns:
            True if the stable_id was successfully injected and kept after reload.

        Example:
            >>> # Generate and inject a new stable_id
            >>> new_id = NodriverClient.generate_stable_id()
            >>> await client.set_stable_id(new_id)
            True

            >>> # Or inject a specific stable_id
            >>> await client.set_stable_id("your-known-stable-id")
            True
        """
        import asyncio

        # Inject stable_id into localStorage
        inject_js = f"""
        (() => {{
            // Clear existing statsig data
            for (let i = localStorage.length - 1; i >= 0; i--) {{
                const key = localStorage.key(i);
                if (key && key.toLowerCase().includes('statsig')) {{
                    localStorage.removeItem(key);
                }}
            }}
            // Set our stable_id
            localStorage.setItem('STATSIG_LOCAL_STORAGE_STABLE_ID', '{stable_id}');
            return localStorage.getItem('STATSIG_LOCAL_STORAGE_STABLE_ID');
        }})()
        """
        try:
            await self._tab.evaluate(inject_js, await_promise=False)

            if reload_page:
                # Reload to reinitialize SDK with our stable_id
                current_url = await self._tab.evaluate("window.location.href", await_promise=False)
                await self._tab.get(current_url if current_url else f"{self.BASE_URL}/imagine")
                await asyncio.sleep(4)

                # Verify stable_id was kept
                current_id = await self.get_stable_id()
                return current_id == stable_id

            return True
        except Exception:
            return False

    async def _navigate_to_post(self, post_id: str) -> None:
        """Navigate to a specific post page."""
        import asyncio

        url = f"{self.BASE_URL}/imagine/post/{post_id}"
        await self._tab.get(url)
        await asyncio.sleep(2)

    async def create_video_from_image(
        self,
        image_url: str,
        parent_post_id: str,
        aspect_ratio: str = "2:3",
        video_length: int = 10,
        statsig_id: str | None = None,
        preset: VideoPreset | str = "normal",
        adjustment_prompt: str | None = None,
        video_resolution: str = "720",
    ) -> VideoGenerationResult:
        """Generate a video from an image using Grok's chat API.

        NodriverClient override: tries to get statsig_id from page context first.

        Args:
            image_url: Source image URL
            parent_post_id: Parent post UUID
            aspect_ratio: Video aspect ratio (default "2:3")
            video_length: Video duration in seconds (default 10)
            statsig_id: Optional style seed for reproducible styles
            preset: Video preset - 'normal', 'fun', or 'spicy'
            adjustment_prompt: Video generation prompt (same as typing in Grok UI after image).
                Can include any instructions: camera movement, character actions, or both.
                Examples: "Static Shot", "she turns her head", "camera zooms in while he walks".
                If provided, overrides preset and uses 'custom' mode.
            video_resolution: Video resolution - "480", "720", or "1080" (default "720")
        """
        # Try to get statsig_id from page context first, then generate if not found
        if statsig_id is None:
            statsig_id = await self._get_statsig_id_from_page()
        if statsig_id is None:
            statsig_id = generate_statsig_id()

        # Build payload using shared utilities
        mode_value = resolve_preset(preset)
        payload = build_video_payload(
            image_url,
            parent_post_id,
            mode_value,
            aspect_ratio,
            video_length,
            adjustment_prompt=adjustment_prompt,
            video_resolution=video_resolution,
        )

        # Get raw text response
        response_text = await self._api_request_text(
            "POST",
            "/rest/app-chat/conversations/new",
            payload,
            extra_headers={"x-statsig-id": statsig_id},
        )

        # Parse using shared utility
        return parse_video_ndjson_response(response_text, parent_post_id, statsig_id)

    # =========================================================================
    # UI Menu Operations (shared helper + specific actions)
    # =========================================================================

    async def _open_post_menu(self, post_id: str) -> bool:
        """
        Navigate to a post and open its "..." menu.

        This is a shared helper for all post menu operations.

        Args:
            post_id: The post UUID to navigate to

        Returns:
            True if menu was opened successfully

        Raises:
            GrokAPIError: If post is 404 or menu button not found
        """
        import asyncio

        d = self._ui_delay

        # Navigate to the post page
        await self._tab.get(f"{self.BASE_URL}/imagine/post/{post_id}")
        await asyncio.sleep(3 * d)

        # Check if page is 404
        page_text = await self._tab.evaluate("document.body.innerText")
        if "Page not found" in page_text or "404" in page_text:
            raise GrokAPIError(f"Post {post_id} not found (404)")

        # Find and click the "..." menu button
        # Note: aria-label varies by post type and language:
        # - Image posts: "更多选项" / "More options"
        # - Video posts: "Options"
        menu_btn = None
        selectors = [
            'button[aria-label="更多选项"][aria-haspopup="menu"]',
            'button[aria-label="More options"][aria-haspopup="menu"]',
            'button[aria-label="Options"][aria-haspopup="menu"]',
            'button[aria-label="Options"]',  # fallback without haspopup
        ]
        for _ in range(3):
            for selector in selectors:
                try:
                    menu_btn = await self._tab.find(selector)
                    if menu_btn:
                        break
                except Exception:
                    pass
            if menu_btn:
                break
            await asyncio.sleep(2 * d)

        if menu_btn is None:
            raise GrokAPIError("Could not find '...' menu button (Options/更多选项)")

        await menu_btn.scroll_into_view()
        await asyncio.sleep(0.5 * d)
        await menu_btn.mouse_click()
        await asyncio.sleep(1 * d)

        return True

    async def _click_menu_item(self, *text_options: str) -> bool:
        """
        Click a menu item by its text (supports multiple language options).

        Uses nodriver's mouse_click() which works better than JS click()
        for React/Radix menu items.

        Args:
            *text_options: One or more text strings to match (e.g., "Save", "保存")

        Returns:
            True if item was clicked

        Raises:
            GrokAPIError: If menu item not found
        """
        import asyncio

        d = self._ui_delay

        # Try to find and click the matching menu item
        for _ in range(3):
            # Get all menu items
            items = await self._tab.find_all('[role="menuitem"]')

            for item in items:
                # Get text property (nodriver elements have a .text property)
                item_text = item.text.strip() if item.text else ""

                if item_text in text_options:
                    await item.scroll_into_view()
                    await asyncio.sleep(0.2 * d)
                    await item.mouse_click()
                    return True

            await asyncio.sleep(1 * d)

        raise GrokAPIError(f"Could not find menu item: {text_options}")

    async def _click_confirm_button(self, *text_options: str) -> bool:
        """
        Click a confirmation button in a dialog.

        Args:
            *text_options: One or more text strings to match

        Returns:
            True if button was clicked

        Raises:
            GrokAPIError: If confirm button not found
        """
        import asyncio

        d = self._ui_delay
        text_list = list(text_options)

        for _ in range(3):
            result = await self._tab.evaluate(f"""
                (function() {{
                    const textOptions = {text_list};
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {{
                        const text = btn.innerText.trim();
                        if (textOptions.includes(text)) {{
                            btn.click();
                            return text;
                        }}
                    }}
                    return null;
                }})()
            """)
            if result:
                return True
            await asyncio.sleep(1 * d)

        raise GrokAPIError(f"Could not find confirm button: {text_options}")

    async def delete_video(self, video_id: str) -> bool:
        """
        Delete a child video by clicking the UI "Delete post" button.

        Navigates to the video page, clicks "..." menu, then "Delete post",
        and confirms in the dialog. Only deletes the specific child video.

        Timing is controlled by self._ui_delay parameter (default: 1.0).

        Args:
            video_id: The child video UUID to delete

        Returns:
            True if deletion was successful (or video already doesn't exist)

        Raises:
            GrokAPIError: If delete button not found or deletion fails
        """
        import asyncio

        d = self._ui_delay

        # Try to open menu (will raise if 404)
        try:
            await self._open_post_menu(video_id)
        except GrokAPIError as e:
            if "404" in str(e):
                return True  # Already deleted
            raise

        # Click "Delete video" menu item (button text varies)
        await self._click_menu_item("删除视频", "删除帖子", "Delete video", "Delete post")
        await asyncio.sleep(1 * d)

        # Confirm deletion (button text in dialog)
        await self._click_confirm_button("删除视频", "删除帖子", "Delete video", "Delete post")
        await asyncio.sleep(1 * d)

        return True

    async def _is_post_favorited(self) -> bool:
        """
        Check if the current post is favorited by examining the menu item text.

        Must be called after _open_post_menu().

        Returns:
            True if post is favorited (shows "取消保存"/"Unsave"), False otherwise
        """
        # Check if "Unsave" menu item exists (means post is favorited)
        is_favorited = await self._tab.evaluate("""
            (() => {
                const items = document.querySelectorAll("[role='menuitem']");
                for (const item of items) {
                    const text = item.innerText.trim();
                    if (text.includes('取消保存') || text.includes('Unsave')) {
                        return true;
                    }
                }
                return false;
            })()
        """)
        return is_favorited

    async def _favorite_post_browser(self, post_id: str) -> bool:
        """
        Internal: Add post to favorites via browser UI (fallback for HTTP 403).

        This method is idempotent - if post is already favorited, it returns True
        without clicking (which would unfavorite it).

        Menu item states:
        - Not favorited: "保存" (Save) with ♡
        - Favorited: "取消保存" (Unsave) with ♥️
        """
        import asyncio

        d = self._ui_delay

        await self._open_post_menu(post_id)
        # Wait for menu to fully render
        await asyncio.sleep(1 * d)

        # Check if already favorited (shows "Unsave")
        if await self._is_post_favorited():
            # Already favorited, close menu and return
            await self._tab.evaluate("document.body.click()")
            await asyncio.sleep(0.5 * d)
            return True

        # Not favorited, click "Save" to favorite
        await self._click_menu_item("保存", "Save")
        await asyncio.sleep(1 * d)

        return True

    async def _unfavorite_post_browser(self, post_id: str) -> bool:
        """
        Internal: Remove post from favorites via browser UI (fallback for HTTP 403).

        This method is idempotent - if post is not favorited, it returns True
        without clicking (which would favorite it).

        Menu item states:
        - Not favorited: "保存" (Save) with ♡
        - Favorited: "取消保存" (Unsave) with ♥️
        """
        import asyncio

        d = self._ui_delay

        await self._open_post_menu(post_id)
        # Wait for menu to fully render
        await asyncio.sleep(1 * d)

        # Check if not favorited (shows "Save" not "Unsave")
        if not await self._is_post_favorited():
            # Already not favorited, close menu and return
            await self._tab.evaluate("document.body.click()")
            await asyncio.sleep(0.5 * d)
            return True

        # Currently favorited, click "Unsave" to unfavorite
        await self._click_menu_item("取消保存", "Unsave")
        await asyncio.sleep(1 * d)

        return True

    async def like_post(self, post_id: str) -> bool:
        """
        Give a thumbs-up to a post via UI menu.

        Note: This is different from favorite_post() which saves to favorites.
        This is the "赞" (Like/thumbs up) action.

        Args:
            post_id: The post UUID to like

        Returns:
            True if like was successful

        Raises:
            GrokAPIError: If post not found or like fails
        """
        import asyncio

        d = self._ui_delay

        await self._open_post_menu(post_id)
        await self._click_menu_item("赞", "Like")
        await asyncio.sleep(1 * d)

        return True

    async def dislike_post(self, post_id: str) -> bool:
        """
        Give a thumbs-down to a post via UI menu.

        Args:
            post_id: The post UUID to dislike

        Returns:
            True if dislike was successful

        Raises:
            GrokAPIError: If post not found or dislike fails
        """
        import asyncio

        d = self._ui_delay

        await self._open_post_menu(post_id)
        await self._click_menu_item("踩", "Dislike")
        await asyncio.sleep(1 * d)

        return True

    async def upgrade_video(self, video_id: str) -> bool:
        """
        Upgrade a video to HD quality via UI menu.

        This triggers the "升级视频" (Upgrade video) option which converts
        a non-HD video to HD quality.

        Args:
            video_id: The video UUID to upgrade

        Returns:
            True if upgrade was initiated successfully

        Raises:
            GrokAPIError: If video not found or upgrade fails
        """
        import asyncio

        d = self._ui_delay

        await self._open_post_menu(video_id)
        await self._click_menu_item("升级视频", "Upgrade video")
        await asyncio.sleep(1 * d)

        return True

    async def get_menu_items(self, post_id: str) -> list[str]:
        """
        Get all available menu items for a post.

        Useful for debugging or checking what actions are available.

        Args:
            post_id: The post UUID

        Returns:
            List of menu item text labels
        """
        import asyncio

        d = self._ui_delay

        await self._open_post_menu(post_id)

        # Get all menu items (use JSON.stringify to avoid nodriver object wrapping)
        import json

        items_json = await self._tab.evaluate("""
            JSON.stringify(
                Array.from(document.querySelectorAll('[role="menuitem"]'))
                    .map(item => item.innerText.trim())
            )
        """)
        items = json.loads(items_json)

        # Close menu by clicking elsewhere
        await self._tab.evaluate("document.body.click()")
        await asyncio.sleep(0.5 * d)

        return items

    async def download_video(self, video_url: str, output_path: Path) -> Path:
        """
        Download a video file using the browser's fetch API.

        This method uses the browser context which has proper Cloudflare clearance,
        making it more reliable than direct HTTP requests.

        Args:
            video_url: The full URL to the video file (media_url or hd_media_url)
            output_path: Destination file path

        Returns:
            Path to the downloaded file

        Raises:
            GrokAPIError: If download fails
        """
        import asyncio
        import base64
        import json as json_module

        # Ensure we're on grok.com (required for proper cookie context)
        current_url = await self._tab.evaluate("window.location.href", await_promise=False)
        if not current_url or "grok.com" not in str(current_url):
            await self._tab.get(f"{self.BASE_URL}/imagine")
            await asyncio.sleep(3)

        # Add dl=1 parameter if not present
        if "?" in video_url:
            download_url = f"{video_url}&dl=1"
        else:
            download_url = f"{video_url}?dl=1"

        # Use browser fetch to download
        # CDN doesn't need credentials, use 'omit' to avoid CORS issues
        js_code = f"""
        (async () => {{
            try {{
                const response = await fetch("{download_url}", {{
                    credentials: 'omit',
                    mode: 'cors'
                }});
                if (!response.ok) {{
                    return JSON.stringify({{
                        "status": response.status,
                        "error": "HTTP " + response.status + " " + response.statusText
                    }});
                }}
                const buffer = await response.arrayBuffer();
                const bytes = new Uint8Array(buffer);
                let binary = '';
                const chunkSize = 8192;
                for (let i = 0; i < bytes.length; i += chunkSize) {{
                    const chunk = bytes.slice(i, i + chunkSize);
                    binary += String.fromCharCode.apply(null, chunk);
                }}
                const base64 = btoa(binary);
                return JSON.stringify({{
                    "status": 200,
                    "data": base64
                }});
            }} catch (e) {{
                return JSON.stringify({{
                    "status": 0,
                    "error": e.message
                }});
            }}
        }})()
        """

        result_str = await self._tab.evaluate(js_code, await_promise=True, return_by_value=True)
        result = json_module.loads(result_str)

        if result["status"] != 200:
            raise GrokAPIError(f"Download failed: {result.get('error', 'Unknown error')}")

        # Decode base64 and write to file
        video_data = base64.b64decode(result["data"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(video_data)

        return output_path

    async def edit_image(
        self, post_id: str, edit_prompt: str, timeout: int = 60
    ) -> ImageEditResult:
        """
        Edit an image to generate new variations.

        This navigates to the post, clicks "编辑图像", enters the prompt,
        and captures the API response with generated images.

        Each edit generates 2 images. Some may be moderated (blocked).

        Args:
            post_id: The post UUID (parent image)
            edit_prompt: The edit instruction (e.g., "add sunglasses", "改成白色丝绸")
            timeout: Max seconds to wait for generation (default 60)

        Returns:
            ImageEditResult with image URLs and moderation info

        Raises:
            GrokAPIError: If edit fails or times out

        Example:
            >>> result = await client.edit_image_via_ui("abc-123", "add wings")
            >>> result.success_count  # Number of non-moderated images
            >>> result.image_urls  # URLs of successful images
            >>> result.has_enough_success(2)  # Check if got at least 2
        """
        import asyncio
        import json as json_mod

        from nodriver import cdp

        d = self._ui_delay

        # Navigate to the post
        await self._navigate_to_post(post_id)

        # Set up network monitoring
        await self._tab.send(cdp.network.enable())

        captured_data = {"conversation_id": None, "images": {}}

        async def handle_response(event: cdp.network.ResponseReceived):
            url = event.response.url
            # Only match the specific video generation endpoint, not conversation list
            if "/app-chat/conversations/new" in url:
                captured_data["request_id"] = event.request_id

        async def handle_loading_finished(event: cdp.network.LoadingFinished):
            if captured_data.get("request_id") == event.request_id:
                try:
                    body_result = await self._tab.send(
                        cdp.network.get_response_body(request_id=event.request_id)
                    )
                    body = body_result[0] if isinstance(body_result, tuple) else str(body_result)

                    # Parse NDJSON response
                    for line in body.strip().split("\n"):
                        if not line:
                            continue
                        try:
                            data = json_mod.loads(line)
                            result = data.get("result", {})

                            # Capture conversation ID
                            if "conversation" in result:
                                captured_data["conversation_id"] = result["conversation"].get(
                                    "conversationId"
                                )

                            # Capture image generation responses
                            response = result.get("response", {})
                            if "streamingImageGenerationResponse" in response:
                                img_resp = response["streamingImageGenerationResponse"]
                                image_id = img_resp.get("imageId")
                                if image_id:
                                    # Update image data (later responses have final status)
                                    captured_data["images"][image_id] = {
                                        "image_id": image_id,
                                        "image_url": img_resp.get("imageUrl", ""),
                                        "moderated": img_resp.get("moderated", False),
                                        "progress": img_resp.get("progress", 0),
                                    }
                        except json_mod.JSONDecodeError:
                            continue
                except Exception:
                    pass

        self._tab.add_handler(cdp.network.ResponseReceived, handle_response)
        self._tab.add_handler(cdp.network.LoadingFinished, handle_loading_finished)

        # Wait for page to load
        await asyncio.sleep(3 * d)

        # Click "编辑图像" button (has aria-label="播放" and text "编辑图像")
        clicked = await self._tab.evaluate("""
            (function() {
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const btn of buttons) {
                    if (btn.innerText.includes('编辑图像')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            })()
        """)
        if not clicked:
            raise GrokAPIError("Could not find '编辑图像' button")

        await asyncio.sleep(2 * d)

        # Fill the edit textarea
        escaped_prompt = edit_prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        fill_result = await self._tab.evaluate(f"""
            (function() {{
                const textarea = document.querySelector('textarea[placeholder*="编辑"]') ||
                               document.querySelector('textarea[aria-label*="编辑"]');
                if (!textarea) return 'not found';

                textarea.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ).set;
                setter.call(textarea, "{escaped_prompt}");
                textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return 'ok';
            }})()
        """)
        if fill_result == "not found":
            raise GrokAPIError("Could not find edit textarea")

        await asyncio.sleep(1 * d)

        # Click the submit button (aria-label="生成视频", text becomes "编辑" after filling)
        submit_clicked = await self._tab.evaluate("""
            (function() {
                const btn = document.querySelector('button[aria-label="生成视频"]');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            })()
        """)
        if not submit_clicked:
            raise GrokAPIError("Could not find submit button")

        # Wait for response with timeout
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            # Check if we have completed images (progress=100)
            completed = [
                img for img in captured_data["images"].values() if img.get("progress") == 100
            ]
            if len(completed) >= 2:  # Edit generates 2 images
                break
            await asyncio.sleep(1)

        # Build result
        images = list(captured_data["images"].values())

        return ImageEditResult(
            post_id=post_id,
            edit_prompt=edit_prompt,
            images=images,
            conversation_id=captured_data.get("conversation_id"),
        )

    async def upload_image(self, image_path: str | Path, timeout: int = 30) -> str:
        """
        Upload a local image to Grok Imagine and create a new post.

        This navigates to grok.com/imagine, uploads the image via the hidden
        file input, and waits for the page to redirect to the new post.

        Args:
            image_path: Path to the local image file (PNG, JPG, etc.)
            timeout: Max seconds to wait for upload and redirect (default 30)

        Returns:
            The post ID of the newly created post.

        Raises:
            FileNotFoundError: If the image file doesn't exist.
            GrokAPIError: If upload fails or times out.

        Example:
            >>> post_id = await client.upload_image("/path/to/photo.jpg")
            >>> # Now use the post_id for video or image generation
            >>> video = await client.create_video("zoom in", source_post_id=post_id)
        """
        import asyncio
        import re

        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        # Navigate to imagine page
        await self._tab.get(f"{self.BASE_URL}/imagine")
        await asyncio.sleep(2)

        # Find the hidden file input
        file_input = await self._tab.select('input[type="file"][name="files"]')
        if not file_input:
            raise GrokAPIError("File input element not found on imagine page")

        # Upload the file
        await file_input.send_file(str(image_path.absolute()))

        # Wait for page to redirect to the new post
        start_time = asyncio.get_event_loop().time()
        post_id = None

        while asyncio.get_event_loop().time() - start_time < timeout:
            current_url = await self._tab.evaluate("window.location.href")
            # URL format: https://grok.com/imagine/post/{post_id}
            match = re.search(r"/imagine/post/([a-f0-9-]+)", current_url)
            if match:
                post_id = match.group(1)
                break
            await asyncio.sleep(0.5)

        if not post_id:
            raise GrokAPIError(
                f"Upload timed out after {timeout}s. " "Page did not redirect to post URL."
            )

        return post_id

    async def _scan_favorited_indices(self) -> list[int]:
        """Scan gallery DOM to find which items have been favorited.

        Gallery items have a save button with aria-label:
        - Non-favorited: "保存" (Save)
        - Favorited: "取消保存" (Unsave)

        Returns:
            List of indices (0-based) of favorited gallery items.
        """
        result = await self._tab.evaluate("""
            (function() {
                const items = document.querySelectorAll('[role="listitem"]');
                const favorited = [];

                items.forEach((item, idx) => {
                    // Look for save button - gallery uses "保存" (Save) / "取消保存" (Unsave)
                    const saveBtn = item.querySelector('button[aria-label*="保存"]') ||
                                   item.querySelector('button[aria-label*="Save"]');
                    if (saveBtn) {
                        const label = saveBtn.getAttribute('aria-label') || '';
                        // "取消保存" or "Unsave" means it's currently favorited
                        // "保存" or "Save" means it's not favorited
                        if (label.includes('取消') || label.toLowerCase().includes('unsave')) {
                            favorited.push(idx);
                        }
                    }
                });

                return favorited;
            })()
        """)
        return list(result) if result else []

    async def create_image(
        self,
        prompt: str,
        aspect_ratio: str = "portrait",
        min_success: int = 1,
        max_scroll: int = 5,
        timeout: int = 120,
        thumbnail_selector: "Callable[[int, Callable[[], Awaitable[list[int]]]], Awaitable[list[int]]] | None" = None,
        progress_callback: "Callable[[int], Awaitable[bool]] | None" = None,
    ) -> ImageGenerationResult:
        """
        Generate images from a text prompt (txt2img).

        This navigates to grok.com/imagine, selects Image mode,
        enters the prompt, and captures generated images via WebSocket.
        Will scroll to generate more images if needed.

        IMPORTANT: Generated images are temporary! They're displayed on screen
        but NOT automatically saved. The gallery disappears on page refresh.
        To save an image, click the save/heart icon in the UI.

        Args:
            prompt: Text description of the image to generate
            aspect_ratio: "portrait" (2:3), "square" (1:1), or "landscape" (3:2)
            min_success: Minimum completed images needed (default 1)
            max_scroll: Maximum scroll attempts to generate more images (default 5)
            timeout: Max seconds to wait for initial generation (default 120)
            thumbnail_selector: Optional async callback to select which images to collect
                post_ids for. Signature: async (count, scan_favorites) -> list[int]
                - count: number of gallery items
                - scan_favorites: async function to detect which items user favorited
                - returns: list of indices to collect post_ids for
                If None (default), no post_ids are collected.
                See grok_web.selectors for pre-built selectors.
            progress_callback: Optional async callback for shared target across workers.
                Signature: async (success_count) -> bool
                - success_count: current number of non-moderated images
                - returns: True to continue scrolling, False to stop early
                Used by BrowserWorkerPool for shared target mode where multiple
                workers contribute to a common success target.

        Returns:
            ImageGenerationResult with job IDs and generation info.
            If thumbnail_selector is None, selected_post_ids will be empty.
            If thumbnail_selector is provided, selected_post_ids will contain
            post_ids for the selected images (for video generation).

        Raises:
            GrokAPIError: If generation fails or times out

        Example:
            >>> # Without selection - just generate
            >>> result = await client.create_image("a cat wearing sunglasses")
            >>> result.success_count  # Number of completed images

            >>> # With manual selection (user favorites in browser)
            >>> from grok_web.selectors import manual_favorite_selector
            >>> result = await client.create_image("a cat", thumbnail_selector=manual_favorite_selector)
            >>> result.selected_post_ids  # Post IDs of favorited images
        """
        import asyncio
        import json as json_mod

        from nodriver import cdp

        d = self._ui_delay

        # Navigate to imagine page (go to blank first to ensure clean state on reused Chrome)
        current_url = await self._tab.evaluate("window.location.href")
        if "grok.com/imagine" in str(current_url):
            # Already on imagine page (reused Chrome) - force full reload
            logger.debug("[create_image] Reused Chrome on imagine page, forcing reload")
            await self._tab.reload()
            await asyncio.sleep(2 * d)
        await self._tab.get(f"{self.BASE_URL}/imagine")
        await asyncio.sleep(3 * d)

        # Set up WebSocket monitoring (imagine page uses wss://grok.com/ws/imagine/listen)
        await self._tab.send(cdp.network.enable())

        captured_data: dict = {"jobs": {}}  # job_id -> job info

        async def handle_ws_frame(event: cdp.network.WebSocketFrameReceived):
            """Capture WebSocket frames from imagine/listen endpoint."""
            try:
                payload = event.response.payload_data
                if not payload:
                    return

                data = json_mod.loads(payload)
                msg_type = data.get("type")

                if msg_type == "json":
                    # Job status update
                    job_id = data.get("job_id")
                    if job_id:
                        # Update or create job entry
                        if job_id not in captured_data["jobs"]:
                            captured_data["jobs"][job_id] = {
                                "image_id": job_id,
                                "image_url": "",
                                "moderated": False,
                                "r_rated": False,
                                "progress": 0,
                                "post_id": "",  # Gallery images are temp, no post_id
                                "prompt": data.get("prompt", ""),
                                "full_prompt": data.get("full_prompt", ""),
                                "model_name": data.get("model_name", ""),
                            }

                        # Update progress
                        progress = data.get("percentage_complete", 0)
                        captured_data["jobs"][job_id]["progress"] = int(progress)

                        # Check for moderation and r_rated
                        if data.get("moderated"):
                            captured_data["jobs"][job_id]["moderated"] = True
                        if data.get("r_rated"):
                            captured_data["jobs"][job_id]["r_rated"] = True

                        # When completed, construct the image URL
                        if data.get("current_status") == "completed":
                            image_id = data.get("image_id", job_id)
                            # Grok uses this URL format for generated images
                            captured_data["jobs"][job_id]["image_url"] = (
                                f"https://imagine-public.x.ai/imagine-public/images/{image_id}.png?cache=1"
                            )
                            captured_data["jobs"][job_id]["model_name"] = data.get("model_name", "")
                            captured_data["jobs"][job_id]["full_prompt"] = data.get(
                                "full_prompt", ""
                            )

                elif msg_type == "image":
                    # Image blob received - we don't store the blob (too large)
                    pass

            except json_mod.JSONDecodeError:
                pass
            except Exception:
                pass

        self._tab.add_handler(cdp.network.WebSocketFrameReceived, handle_ws_frame)

        # Step 1: Click the mode dropdown button (aria-label="模型选择")
        model_btn = await self._tab.select('button[aria-label="模型选择"]')
        if model_btn:
            await model_btn.mouse_click()
            await asyncio.sleep(1 * d)

        # Step 2: Select "Image/图片" mode from the Radix dropdown menu
        await self._tab.evaluate("""
            (function() {
                const popper = document.querySelector('[data-radix-popper-content-wrapper]');
                if (!popper) return 'no menu';

                const menuItems = popper.querySelectorAll('[role="menuitem"]');
                for (const item of menuItems) {
                    if (item.innerText.includes('图片') ||
                        item.innerText.includes('Image') ||
                        item.innerText.includes('图像')) {
                        item.click();
                        return 'clicked image';
                    }
                }
                return 'not found';
            })()
        """)
        await asyncio.sleep(1 * d)

        # Step 3: Select aspect ratio if needed (reopen menu)
        aspect_map = {"portrait": 0, "square": 1, "landscape": 2}
        aspect_index = aspect_map.get(aspect_ratio, 0)

        model_btn = await self._tab.select('button[aria-label="模型选择"]')
        if model_btn:
            await model_btn.mouse_click()
            await asyncio.sleep(1 * d)

        await self._tab.evaluate(f"""
            (function() {{
                const popper = document.querySelector('[data-radix-popper-content-wrapper]');
                if (!popper) return 'no menu';

                const buttons = popper.querySelectorAll('button');
                if (buttons.length > {aspect_index}) {{
                    buttons[{aspect_index}].click();
                    return 'clicked aspect ' + {aspect_index};
                }}
                return 'no aspect buttons';
            }})()
        """)
        await asyncio.sleep(0.5 * d)

        # Close menu
        await self._tab.evaluate("document.body.click()")
        await asyncio.sleep(0.5 * d)

        # Step 4: Fill the prompt input (TipTap/ProseMirror contenteditable div)
        escaped_prompt = prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        logger.debug(f"[create_image] Filling prompt ({len(prompt)} chars)")
        fill_result = await self._tab.evaluate(f"""
            (function() {{
                const editor = document.querySelector('.tiptap.ProseMirror') ||
                               document.querySelector('[contenteditable="true"]') ||
                               document.querySelector('.ProseMirror');
                if (!editor) return 'not found';

                editor.focus();
                editor.innerHTML = '<p>{escaped_prompt}</p>';
                editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                return 'ok';
            }})()
        """)
        if fill_result == "not found":
            raise GrokAPIError("Could not find prompt editor on imagine page")
        logger.debug(f"[create_image] Prompt filled: {fill_result}")

        await asyncio.sleep(1 * d)

        # Step 5: Click the submit button
        logger.debug("[create_image] Looking for submit button...")
        submit_btn = await self._tab.select('button[aria-label="提交"]')
        if submit_btn:
            logger.debug("[create_image] Found submit button, clicking...")
            await submit_btn.mouse_click()
            logger.debug("[create_image] Submit button clicked")
        else:
            raise GrokAPIError("Could not find submit button")

        # Step 6: Wait for initial batch of images via WebSocket
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            completed = [
                job for job in captured_data["jobs"].values() if job.get("progress") == 100
            ]
            # Wait for at least 6 images (first batch is usually 6)
            if len(completed) >= 6:
                break
            await asyncio.sleep(1)

        # Check if we got any jobs at all - if not, something went wrong
        if len(captured_data["jobs"]) == 0:
            logger.error(
                "[create_image] No jobs received after initial wait. Navigation or WebSocket may have failed."
            )
            raise GrokAPIError(
                "No image generation jobs received. The page may not have loaded correctly."
            )

        # Step 7: Scroll down to generate more if needed
        # min_success means non-moderated images, so we keep scrolling until we have enough
        # Note: Grok rate-limits generation - new batches appear every 2-3 minutes
        # We use exponential backoff when scroll doesn't generate new jobs
        scroll_count = 0
        jobs_before_scroll = 0
        consecutive_no_new_jobs = 0
        while scroll_count < max_scroll:
            # Wait until ALL current jobs have completed (progress=100)
            # This ensures moderated status has been received for all images
            prev_job_count = 0
            stable_count = 0
            stable_wait_start = asyncio.get_event_loop().time()
            max_stable_wait = 30  # Max 30 seconds to wait for stability

            while stable_count < 3:  # Wait for 3 consecutive stable checks
                # Timeout check
                if asyncio.get_event_loop().time() - stable_wait_start > max_stable_wait:
                    logger.debug("[scroll] stable wait timeout after 30s")
                    break

                all_jobs = list(captured_data["jobs"].values())
                completed = [job for job in all_jobs if job.get("progress") == 100]

                # Check if all jobs are completed and count is stable
                if len(completed) == len(all_jobs) and len(all_jobs) > 0:
                    if len(all_jobs) == prev_job_count:
                        stable_count += 1
                    else:
                        stable_count = 0
                    prev_job_count = len(all_jobs)
                else:
                    stable_count = 0
                    prev_job_count = len(all_jobs)

                await asyncio.sleep(1)

            # Now count non-moderated (successful) images
            completed = [
                job for job in captured_data["jobs"].values() if job.get("progress") == 100
            ]
            success_count = sum(1 for job in completed if not job.get("moderated"))
            moderated_count = sum(1 for job in completed if job.get("moderated"))

            logger.info(
                f"[scroll {scroll_count}] jobs={len(completed)}, success={success_count}, moderated={moderated_count}, target={min_success}"
            )

            # Check shared target callback (used by pool for multi-worker coordination)
            if progress_callback is not None:
                should_continue = await progress_callback(success_count)
                if not should_continue:
                    logger.info(
                        f"[scroll] progress_callback signaled stop at {success_count} success"
                    )
                    break

            if success_count >= min_success:
                logger.info(f"[scroll] reached min_success={min_success}, stopping")
                break

            # Check if scrolling is generating new jobs
            if scroll_count > 0 and len(completed) == jobs_before_scroll:
                consecutive_no_new_jobs += 1
                logger.warning(
                    f"[scroll] no new jobs after scroll {scroll_count}, jobs still at {len(completed)} (consecutive: {consecutive_no_new_jobs})"
                )
            else:
                consecutive_no_new_jobs = 0  # Reset when new jobs appear
            jobs_before_scroll = len(completed)

            # Exponential backoff when scroll doesn't generate new jobs
            # Grok rate-limits generation, new batches appear every 2-3 minutes
            if consecutive_no_new_jobs >= 3:
                # Wait longer before next scroll (15s, then 30s, capped at 60s)
                backoff_wait = min(15 * (2 ** (consecutive_no_new_jobs - 3)), 60)
                logger.info(f"[scroll] rate-limited, waiting {backoff_wait}s before next scroll")
                await asyncio.sleep(backoff_wait)

            # Scroll down to trigger more generation
            # The imagine page uses a specific scrollable container, not window
            scroll_result = await self._tab.evaluate("""
                (function() {
                    // Find the scrollable container (has overflow-scroll class)
                    const container = document.querySelector('.overflow-scroll') ||
                                     document.querySelector('[class*="overflow-scroll"]') ||
                                     document.querySelector('main');
                    if (container) {
                        const beforeScroll = container.scrollTop;
                        container.scrollTop = container.scrollHeight;
                        return 'scrolled container from ' + beforeScroll + ' to ' + container.scrollTop + ' (max: ' + container.scrollHeight + ')';
                    }
                    // Fallback to window scroll
                    window.scrollTo(0, document.body.scrollHeight);
                    return 'scrolled window (fallback)';
                })()
            """)
            logger.debug(f"[scroll {scroll_count}] {scroll_result}")
            await asyncio.sleep(3 * d)  # Brief wait for scroll to trigger new jobs
            scroll_count += 1

        # Build result
        images = list(captured_data["jobs"].values())
        selected_post_ids: list[str] = []

        # Step 8: Collect post_ids via thumbnail_selector callback
        # When user clicks "Create Video" on a gallery image, Grok auto-favorites it
        # by sending POST /rest/media/post/like with {"id": "post_id"}
        # We capture these requests to get post_ids without navigation
        if thumbnail_selector and images:
            await asyncio.sleep(2 * d)  # Wait for DOM to settle

            # Set up request capture for /rest/media/post/like
            captured_like_ids: list[str] = []

            async def capture_like_request(event: cdp.network.RequestWillBeSent):
                """Capture POST /rest/media/post/like to get post_ids."""
                url = event.request.url
                if MEDIA_POST_LIKE_ENDPOINT in url:
                    logger.info(f"[post_id_capture] Detected like request: {url}")
                    post_data = getattr(event.request, "post_data", None)
                    has_post_data = getattr(event.request, "has_post_data", False)
                    logger.debug(
                        f"[post_id_capture] has_post_data={has_post_data}, "
                        f"post_data={'present' if post_data else 'None'}"
                    )

                    # Try to get post_data from request body
                    if not post_data and has_post_data:
                        # post_data may be empty in RequestWillBeSent, try to fetch it
                        try:
                            result = await self._tab.send(
                                cdp.network.get_request_post_data(event.request_id)
                            )
                            post_data = result
                            logger.debug(
                                f"[post_id_capture] Fetched post_data: {post_data[:100] if post_data else 'None'}..."
                            )
                        except Exception as e:
                            logger.warning(f"[post_id_capture] Failed to get post data: {e}")

                    if post_data:
                        try:
                            import re

                            # Try JSON parse first
                            data = json_mod.loads(post_data)
                            post_id = data.get("id")
                            if post_id and post_id not in captured_like_ids:
                                captured_like_ids.append(post_id)
                                logger.info(f"[post_id_capture] Captured post_id: {post_id}")
                        except json_mod.JSONDecodeError:
                            # Fallback: regex extraction
                            match = re.search(r'"id"\s*:\s*"([^"]+)"', post_data)
                            if match:
                                post_id = match.group(1)
                                if post_id not in captured_like_ids:
                                    captured_like_ids.append(post_id)
                                    logger.info(
                                        f"[post_id_capture] Captured post_id (regex): {post_id}"
                                    )

            self._tab.add_handler(cdp.network.RequestWillBeSent, capture_like_request)

            # Get count of gallery items
            item_count_result = await self._tab.evaluate(
                "document.querySelectorAll('[role=\"listitem\"]').length"
            )
            item_count = int(item_count_result) if item_count_result else 0

            # Call the selector callback
            # For manual selection: user clicks "Create Video" in browser, we capture post_ids
            # The callback can wait for user input (e.g., signal file, keyboard input)
            # then return indices (which we ignore - we use captured_like_ids instead)
            await thumbnail_selector(item_count, self._scan_favorited_indices)

            # Use captured post_ids from /rest/media/post/like requests
            selected_post_ids = captured_like_ids

        return ImageGenerationResult(
            prompt=prompt,
            images=images,
            conversation_id=None,  # Not available via WebSocket
            selected_post_ids=selected_post_ids,
        )

    async def create_video_from_text(
        self,
        prompt: str,
        aspect_ratio: str = "portrait",
        timeout: int = 300,
        wait_for_video: bool = True,
    ) -> VideoGenerationResult:
        """
        Generate video from text prompt (txt2vid).

        This navigates to grok.com/imagine with Video mode (default),
        enters the prompt, and waits for the video to finish generating.

        Note: txt2vid creates a SINGLE video post, not a gallery.
        The URL redirects quickly but video takes ~25-30 seconds to render.

        Args:
            prompt: Text description of the video to generate
            aspect_ratio: "portrait" (9:16), "square" (1:1), or "landscape" (16:9)
            timeout: Max seconds to wait for video generation (default 300).
                    While txt2vid is usually fast (~30s), we use a generous timeout
                    to handle network delays and server-side queueing.
            wait_for_video: Wait for video element to load (default True).
                           Set False to return immediately after URL redirect.

        Returns:
            VideoGenerationResult with video_id (the post ID from redirect URL)

        Raises:
            GrokAPIError: If generation fails or times out

        Example:
            >>> result = await client.create_video_from_text("a cat playing with yarn")
            >>> result.video_id  # Generated video post UUID
            >>> result.web_url   # URL to view the video
        """
        import asyncio
        import re

        d = self._ui_delay

        # Navigate to imagine page (default is Video mode)
        await self._tab.get(f"{self.BASE_URL}/imagine")
        await asyncio.sleep(3 * d)

        # Step 1: Verify we're in Video mode (should be default)
        model_btn = await self._tab.select('button[aria-label="模型选择"]')
        if model_btn:
            mode_text = await self._tab.evaluate(
                'document.querySelector(\'button[aria-label="模型选择"]\')?.innerText || ""'
            )
            # If showing "图片", switch to "视频"
            if "图片" in mode_text or "Image" in mode_text:
                await model_btn.mouse_click()
                await asyncio.sleep(1 * d)

                await self._tab.evaluate("""
                    (function() {
                        const popper = document.querySelector('[data-radix-popper-content-wrapper]');
                        if (!popper) return 'no menu';

                        const menuItems = popper.querySelectorAll('[role="menuitem"]');
                        for (const item of menuItems) {
                            if (item.innerText.includes('视频') ||
                                item.innerText.includes('Video')) {
                                item.click();
                                return 'clicked video';
                            }
                        }
                        return 'not found';
                    })()
                """)
                await asyncio.sleep(1 * d)

        # Step 2: Select aspect ratio if needed
        aspect_map = {"portrait": 0, "square": 1, "landscape": 2}
        aspect_index = aspect_map.get(aspect_ratio, 0)

        model_btn = await self._tab.select('button[aria-label="模型选择"]')
        if model_btn:
            await model_btn.mouse_click()
            await asyncio.sleep(1 * d)

        await self._tab.evaluate(f"""
            (function() {{
                const popper = document.querySelector('[data-radix-popper-content-wrapper]');
                if (!popper) return 'no menu';

                const buttons = popper.querySelectorAll('button');
                if (buttons.length > {aspect_index}) {{
                    buttons[{aspect_index}].click();
                    return 'clicked aspect ' + {aspect_index};
                }}
                return 'no aspect buttons';
            }})()
        """)
        await asyncio.sleep(0.5 * d)

        # Close menu
        await self._tab.evaluate("document.body.click()")
        await asyncio.sleep(0.5 * d)

        # Step 3: Fill the prompt input
        escaped_prompt = prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        fill_result = await self._tab.evaluate(f"""
            (function() {{
                const editor = document.querySelector('.tiptap.ProseMirror') ||
                               document.querySelector('[contenteditable="true"]') ||
                               document.querySelector('.ProseMirror');
                if (!editor) return 'not found';

                editor.focus();
                editor.innerHTML = '<p>{escaped_prompt}</p>';
                editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                return 'ok';
            }})()
        """)
        if fill_result == "not found":
            raise GrokAPIError("Could not find prompt editor on imagine page")

        await asyncio.sleep(1 * d)

        # Step 4: Click the submit button
        submit_btn = await self._tab.select('button[aria-label="提交"]')
        if submit_btn:
            await submit_btn.mouse_click()
        else:
            raise GrokAPIError("Could not find submit button")

        # Step 5: Wait for URL to change to /imagine/post/{id}
        start_time = asyncio.get_event_loop().time()
        post_id = None

        while asyncio.get_event_loop().time() - start_time < timeout:
            current_url = self._tab.target.url
            # Extract post ID from URL like https://grok.com/imagine/post/{uuid}
            match = re.search(r"/imagine/post/([a-f0-9-]+)", current_url)
            if match:
                post_id = match.group(1)
                break
            await asyncio.sleep(1)

        if not post_id:
            raise GrokAPIError("Timeout waiting for video generation redirect")

        # Step 6: Optionally wait for video element to actually load
        # (URL redirects quickly but video takes ~25-30s to render)
        video_ready = False
        if wait_for_video:
            remaining_time = timeout - (asyncio.get_event_loop().time() - start_time)
            wait_start = asyncio.get_event_loop().time()

            while asyncio.get_event_loop().time() - wait_start < remaining_time:
                video_info = await self._tab.evaluate("""
                    (function() {
                        const videos = document.querySelectorAll('video');
                        if (videos.length === 0) return {found: false};

                        // Check if any video has loaded (readyState >= 2 = HAVE_CURRENT_DATA)
                        for (const v of videos) {
                            if (v.readyState >= 2 && v.duration > 0) {
                                return {
                                    found: true,
                                    duration: v.duration,
                                    src: v.src || ''
                                };
                            }
                        }
                        return {found: false};
                    })()
                """)

                # Handle nodriver list format
                found = False
                if isinstance(video_info, dict):
                    found = video_info.get("found", False)
                elif isinstance(video_info, list):
                    for item in video_info:
                        if item[0] == "found" and item[1].get("value"):
                            found = True
                            break

                if found:
                    video_ready = True
                    break

                await asyncio.sleep(1)

        # Build result - for txt2vid, the post_id IS the video
        return VideoGenerationResult(
            video_id=post_id,
            parent_post_id=post_id,
            moderated=False,  # If we got a redirect, it wasn't moderated
            progress=100 if video_ready or not wait_for_video else 50,
            mode="text",  # txt2vid mode
        )

    # =========================================================================
    # Unified Video Generation API (for pool compatibility)
    # =========================================================================

    @overload
    async def create_video(
        self,
        prompt: str,
        *,
        aspect_ratio: str = ...,
        timeout: int = ...,
        wait_for_video: bool = ...,
    ) -> VideoGenerationResult:
        """txt2vid: Generate video from text prompt only."""
        ...

    @overload
    async def create_video(
        self,
        prompt: str,
        *,
        source_post_id: str,
        preset: VideoPreset | str = ...,
        aspect_ratio: str = ...,
        timeout: int = ...,
        duration: int = ...,
        resolution: str = ...,
    ) -> VideoGenerationResult:
        """img2vid: Generate video with custom prompt."""
        ...

    @overload
    async def create_video(
        self,
        *,
        source_post_id: str,
        preset: VideoPreset | str,
        aspect_ratio: str = ...,
        timeout: int = ...,
        duration: int = ...,
        resolution: str = ...,
    ) -> VideoGenerationResult:
        """img2vid: Generate video with preset only (no prompt)."""
        ...

    @overload
    async def create_video(
        self,
        prompt: str,
        *,
        source_image_path: str | Path,
        aspect_ratio: str = ...,
        timeout: int = ...,
        duration: int = ...,
        resolution: str = ...,
    ) -> VideoGenerationResult:
        """upload2vid: Upload local image and generate video from it."""
        ...

    async def create_video(
        self,
        prompt: str = "",
        *,
        source_post_id: str | None = None,
        source_image_path: str | Path | None = None,
        preset: VideoPreset | str = "normal",
        aspect_ratio: str = "portrait",
        timeout: int = 300,
        wait_for_video: bool = True,
        duration: int = 10,
        resolution: str = "720p",
    ) -> VideoGenerationResult:
        """
        Unified video generation API supporting multiple modes.

        The mode is automatically detected based on which source parameter is provided:
        - No source → txt2vid (generate video from text prompt)
        - source_post_id → img2vid (generate video from existing Grok image)
        - source_image_path → upload2vid (upload local image and generate video)

        Args:
            prompt: For txt2vid: full video description.
                   For img2vid/upload2vid: adjustment instructions (camera, motion, style).

            source_post_id: (img2vid) Existing Grok image post ID to animate.
            source_image_path: (upload2vid) Local image path to upload and animate.

            preset: Video style preset - 'normal', 'fun', or 'spicy'.
                   Only used for img2vid mode.
            aspect_ratio: Video aspect ratio.
            timeout: Max seconds to wait for video generation (default 300).
            wait_for_video: (txt2vid only) Wait for video element to load (default True).
            duration: Video duration in seconds (default 10). Options: 6, 10.
            resolution: Video resolution (default "720p"). Options: "480p", "720p".

        Returns:
            VideoGenerationResult with video_id and metadata.
        """
        # Validate: cannot specify both sources
        if source_post_id is not None and source_image_path is not None:
            raise ValueError("Cannot specify both source_post_id and source_image_path.")

        # Mode: upload2vid (upload local image, then generate video)
        if source_image_path is not None:
            # Upload the image first to create a post
            uploaded_post_id = await self.upload_image(source_image_path)
            # Then generate video from the uploaded post
            return await self.create_video_via_ui(
                parent_post_id=uploaded_post_id,
                preset=preset,
                timeout=timeout,
                adjustment_prompt=prompt if prompt else None,
                duration=duration,
                resolution=resolution,
            )

        # Mode: img2vid (from existing Grok image post)
        if source_post_id is not None:
            return await self.create_video_via_ui(
                parent_post_id=source_post_id,
                preset=preset,
                timeout=timeout,
                adjustment_prompt=prompt if prompt else None,
                duration=duration,
                resolution=resolution,
            )

        # Mode: txt2vid (from text prompt only)
        return await self.create_video_from_text(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            timeout=timeout,
            wait_for_video=wait_for_video,
        )

    async def create_video_via_ui(
        self,
        parent_post_id: str,
        preset: VideoPreset | str = VideoPreset.NORMAL,
        timeout: int = 300,
        stable_id: str | None = None,
        adjustment_prompt: str | None = None,
        duration: int = 10,
        resolution: str = "720p",
    ) -> VideoGenerationResult:
        """
        Generate video by simulating UI button click (more reliable for anti-bot bypass).

        This navigates to the post page, selects the preset, and clicks "Create Video",
        using the same code path as manual user interaction.

        Args:
            parent_post_id: The image post ID to generate video from
            preset: Video style preset - 'normal', 'fun', or 'spicy' (or VideoPreset enum)
            timeout: Max seconds to wait for video generation (default 300).
                    Video generation typically takes 2-5 minutes for img2vid mode.
            stable_id: Optional custom stable_id to inject before generation.
                      Use generate_stable_id() to create one. Controls A/B style bucket.
            adjustment_prompt: Video generation prompt (same as typing in Grok UI after image).
                      Can include any instructions: camera movement, character actions, or both.
                      Examples: "Static Shot", "she turns her head", "camera zooms in while he walks".
                      When provided, overrides preset and sets result.mode='custom'.
            duration: Video duration in seconds (default 10). Options: 6, 10.
            resolution: Video resolution (default "720p"). Options: "480p", "720p".

        Returns:
            VideoGenerationResult with video_id (may be empty if moderated).
            When adjustment_prompt is used, result.mode will be 'custom'.
        """
        import asyncio

        from nodriver import cdp

        # Inject custom stable_id if provided
        if stable_id:
            await self.set_stable_id(stable_id, reload_page=False)

        # Normalize preset to string
        preset_str = preset.value if isinstance(preset, VideoPreset) else str(preset).lower()

        # Map preset string to menu text (case-sensitive as shown in UI)
        preset_menu_map = {
            "normal": "Normal",
            "fun": "Fun",
            "spicy": "Spicy",
        }
        preset_menu_text = preset_menu_map.get(preset_str, "Normal")

        # Navigate to the post page (this reloads with our stable_id)
        await self._navigate_to_post(parent_post_id)

        # Set up network monitoring to capture the response and statsig_id
        await self._tab.send(cdp.network.enable())

        captured_response = {"body": None, "request_id": None, "statsig_id": None}

        async def handle_request(event: cdp.network.RequestWillBeSent):
            url = event.request.url
            # Only match the specific video generation endpoint, not conversation list
            if "/app-chat/conversations/new" in url:
                captured_response["request_id"] = event.request_id
                # Capture statsig_id from request headers
                headers = event.request.headers
                # Headers can be dict or special CDP type
                if headers and (hasattr(headers, "get") or isinstance(headers, dict)):
                    captured_response["statsig_id"] = headers.get("x-statsig-id")

        async def handle_loading_finished(event: cdp.network.LoadingFinished):
            if (
                captured_response["request_id"]
                and captured_response["request_id"] == event.request_id
            ):
                try:
                    body_result = await self._tab.send(
                        cdp.network.get_response_body(request_id=event.request_id)
                    )
                    # CDP returns a tuple (body, base64_encoded)
                    if isinstance(body_result, tuple):
                        body = body_result[0]
                    else:
                        body = getattr(body_result, "body", str(body_result))
                    captured_response["body"] = body
                except Exception:
                    pass  # Response body may not be available

        self._tab.add_handler(cdp.network.RequestWillBeSent, handle_request)
        self._tab.add_handler(cdp.network.LoadingFinished, handle_loading_finished)

        # Wait for page to fully load (React hydration) + random jitter
        await asyncio.sleep(3 + random.uniform(0, 2.0))

        # Scroll down to reveal the "Create Video" button (it's below the image)
        await self._tab.evaluate(
            "window.scrollTo(0, document.body.scrollHeight / 2)", await_promise=False
        )
        await asyncio.sleep(1 + random.uniform(0, 0.5))

        # If adjustment_prompt is provided, fill the textarea using React-compatible method
        if adjustment_prompt:
            # Escape the prompt for JavaScript
            escaped_prompt = (
                adjustment_prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            )

            # React-compatible textarea filling:
            # 1. Use native setter to bypass React's controlled input
            # 2. Dispatch 'input' event to trigger React's onChange
            fill_textarea_js = f"""
            (function() {{
                // Find the textarea by aria-label (Chinese UI: "制作视频", English: "Make video")
                const textarea = document.querySelector('textarea[aria-label="制作视频"]') ||
                                 document.querySelector('textarea[aria-label="Make video"]') ||
                                 document.querySelector('textarea[placeholder*="视频"]') ||
                                 document.querySelector('textarea[placeholder*="video"]');

                if (!textarea) {{
                    return 'textarea_not_found';
                }}

                // Focus the textarea first
                textarea.focus();

                // Use native setter to set value (bypasses React's controlled input)
                const nativeTextAreaValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ).set;
                nativeTextAreaValueSetter.call(textarea, "{escaped_prompt}");

                // Dispatch input event to trigger React's onChange handler
                const inputEvent = new Event('input', {{ bubbles: true, cancelable: true }});
                textarea.dispatchEvent(inputEvent);

                // Also dispatch change event for good measure
                const changeEvent = new Event('change', {{ bubbles: true, cancelable: true }});
                textarea.dispatchEvent(changeEvent);

                return 'success';
            }})()
            """

            fill_result = await self._tab.evaluate(fill_textarea_js, await_promise=False)
            if fill_result == "textarea_not_found":
                # Try alternative: look for any visible textarea
                fill_alt_js = f"""
                (function() {{
                    const textareas = Array.from(document.querySelectorAll('textarea'));
                    // Find visible textarea
                    const visible = textareas.find(t => t.offsetParent !== null);
                    if (!visible) return 'no_visible_textarea';

                    visible.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(visible, "{escaped_prompt}");
                    visible.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    visible.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return 'success_alt';
                }})()
                """
                fill_result = await self._tab.evaluate(fill_alt_js, await_promise=False)

            # Wait for React to process the state update
            await asyncio.sleep(0.5)

            # When using adjustment_prompt, skip preset selection and go directly to generate button
            # (adjustment_prompt uses custom mode which overrides preset)

        # Open "视频选项" (Video Options) dropdown to set duration, resolution, and preset.
        # The dropdown trigger is button[aria-label="视频选项"].
        # Inside the Radix dropdown menu:
        #   - Duration: button[aria-label="6s"] / button[aria-label="10s"]
        #   - Resolution: button[aria-label="480p"] / button[aria-label="720p"]
        #   - Preset: role="menuitem" with text "Spicy" / "Fun" / "Normal"
        # IMPORTANT: This is a Radix dropdown — clicking ANY item closes the menu.
        # We must reopen the dropdown between each selection.
        preset_selected = False

        async def _open_video_options():
            """Open the Video Options dropdown. Returns True if opened."""
            btn = await self._tab.find('button[aria-label="视频选项"]')
            if not btn:
                btn = await self._tab.find('button[aria-label="Video Options"]')
            if btn:
                await btn.mouse_click()
                await asyncio.sleep(0.5)
                return True
            return False

        try:
            # Select duration (e.g., "10s") — open menu, click, menu closes
            if await _open_video_options():
                duration_label = f"{duration}s"
                dur_btn = await self._tab.find(f'button[aria-label="{duration_label}"]')
                if dur_btn:
                    await dur_btn.mouse_click()
                    await asyncio.sleep(0.3)

            # Select resolution (e.g., "720p") — reopen menu, click, menu closes
            if await _open_video_options():
                res_label = resolution if resolution.endswith("p") else f"{resolution}p"
                res_btn = await self._tab.find(f'button[aria-label="{res_label}"]')
                if res_btn:
                    await res_btn.mouse_click()
                    await asyncio.sleep(0.3)

            # Select preset if non-normal (clicking preset auto-generates video)
            if not adjustment_prompt and preset_str != "normal" and await _open_video_options():
                menu_items = await self._tab.find_all('[role="menuitem"]')
                for item in menu_items:
                    item_text = item.text.strip() if hasattr(item, "text") else ""
                    if not item_text:
                        idx = menu_items.index(item)
                        item_text = await self._tab.evaluate(
                            f"document.querySelectorAll('[role=\"menuitem\"]')[{idx}].textContent.trim()",
                            await_promise=False,
                        )
                    if item_text == preset_menu_text:
                        await item.mouse_click()

                        # Wait for preset click to trigger API request
                        preset_start = asyncio.get_event_loop().time()
                        while captured_response["request_id"] is None:
                            elapsed = asyncio.get_event_loop().time() - preset_start
                            if elapsed > 3:
                                break
                            await asyncio.sleep(0.3)

                        if captured_response["request_id"] is not None:
                            preset_selected = True
                        break
        except Exception:
            pass  # If dropdown interaction fails, fall back to clicking Create Video

        # For Normal preset (or if preset selection failed), click "Create Video" button
        # with retry logic for UI non-determinism
        if not preset_selected:
            max_click_retries = 3
            click_wait_timeout = 8  # Wait 8s for request capture before retry

            async def find_and_click_create_button() -> bool:
                """Find and click the Create Video button. Returns True if clicked."""
                try:
                    buttons = await self._tab.find_all("button, [role='button']")

                    # Find the "生成视频" / "Create Video" button
                    create_btn = None
                    for btn in buttons:
                        text = ""
                        label = btn.attrs.get("aria-label", "")
                        if hasattr(btn, "text"):
                            text = btn.text.strip() if btn.text else ""

                        if (
                            "生成视频" in text
                            or "重新生成" in text
                            or "Create Video" in text
                            or "Regenerate" in text
                            or "Make video" in label
                            or label == "生成视频"
                            or label == "重新生成"
                        ):
                            create_btn = btn
                            break

                    if not create_btn:
                        return False

                    await create_btn.mouse_click()
                    return True

                except Exception:
                    return False

            # Retry clicking the button if no request is captured
            for click_attempt in range(1, max_click_retries + 1):
                # Reset request_id for each attempt to detect new requests
                captured_response["request_id"] = None

                # Random pre-click delay (human-like)
                await asyncio.sleep(random.uniform(0.3, 0.8))

                clicked = await find_and_click_create_button()
                if not clicked:
                    if click_attempt == max_click_retries:
                        raise GrokAPIError("Could not find 'Create Video' button after retries")
                    await asyncio.sleep(2 + random.uniform(0, 1.0))
                    continue

                # Wait for request to be captured (indicates button click triggered API call)
                click_start = asyncio.get_event_loop().time()
                while captured_response["request_id"] is None:
                    elapsed = asyncio.get_event_loop().time() - click_start
                    if elapsed > click_wait_timeout:
                        break
                    await asyncio.sleep(0.5)

                # If request was captured, break out of retry loop
                if captured_response["request_id"] is not None:
                    break

                # No request captured - wait before retry to let page stabilize
                if click_attempt < max_click_retries:
                    await asyncio.sleep(2 + random.uniform(0, 1.5))

            # If still no request captured after all retries, raise error
            if captured_response["request_id"] is None:
                raise GrokAPIError(
                    f"Button click did not trigger video generation request after {max_click_retries} attempts"
                )

        # Wait for response body with timeout
        start_time = asyncio.get_event_loop().time()
        while captured_response["body"] is None:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                raise GrokAPIError("Timeout waiting for video generation response")
            await asyncio.sleep(0.5)

        # Parse response using shared utility (statsig_id captured from request)
        return parse_video_ndjson_response(
            captured_response["body"], parent_post_id, statsig_id=captured_response["statsig_id"]
        )


# =============================================================================
# SmartGrokClient - Recommended client (delegates to NodriverClient)
# =============================================================================


class SmartGrokClient:
    """
    Recommended client - wraps NodriverClient with cookie loading.

    Provides interactive login setup if cookies are missing, then
    delegates all operations to NodriverClient.

    Example:
        async with SmartGrokClient() as client:
            posts = await client.list_posts()
            video = await client.create_video("zoom in", source_post_id=post_id)
    """

    BASE_URL = "https://grok.com"

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
        browser_host: str | None = None,
        browser_port: int | None = None,
        browser_headless: bool = False,
    ):
        """
        Initialize SmartGrokClient.

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Defaults to ~/.grok-config.json
            browser_host: Remote debugging host. Defaults to "127.0.0.1".
            browser_port: Remote debugging port. None = auto-assigned by ai-dev-browser.
            browser_headless: Run browser in headless mode (default: False)
        """
        self._provided_cookies = cookies  # Store for deferred loading
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.cookies: GrokCookies | None = None  # Will be set in __aenter__
        self._browser_client: NodriverClient | None = None
        self._browser_host = browser_host
        self._browser_port = browser_port
        self._browser_headless = browser_headless

    async def __aenter__(self):
        """Load cookies and initialize NodriverClient."""
        # Load cookies (with auto-setup if missing)
        if self._provided_cookies is not None:
            self.cookies = self._provided_cookies
        else:
            self.cookies = await self._load_or_setup_cookies()

        self._browser_client = NodriverClient(
            cookies=self.cookies,
            config_path=self._config_path,
            host=self._browser_host,
            port=self._browser_port,
            headless=self._browser_headless,
        )
        await self._browser_client.__aenter__()
        return self

    async def _load_or_setup_cookies(self) -> GrokCookies:
        """Load cookies from config, or trigger interactive setup if missing."""
        from .exceptions import GrokConfigError

        try:
            config = load_config(self._config_path)
            return config["cookies"]
        except GrokConfigError:
            # No valid config - trigger interactive setup
            print("⚠️  No valid Grok cookies found. Starting interactive login...")
            from .auth_manager import AuthManager

            auth = AuthManager(config_path=self._config_path)
            success = await auth.setup_auth(timeout_minutes=5, headless=False)

            if not success:
                raise GrokConfigError(
                    "Authentication setup failed.\n"
                    "Please run: python -m grok_web.auth_manager setup"
                ) from None

            # Reload config after successful setup
            config = load_config(self._config_path)
            return config["cookies"]

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any):
        """Clean up browser client."""
        if self._browser_client:
            await self._browser_client.__aexit__(exc_type, exc_val, exc_tb)

    # =========================================================================
    # Read APIs - delegate to NodriverClient
    # =========================================================================

    async def list_posts(
        self, limit: int = 10, source: str | None = "favorites", include_raw_data: bool = False
    ):
        """List posts.

        Args:
            limit: Maximum number of posts to return
            source: Filter by source type:
                - "favorites": Your saved/favorited posts (default)
                - None: All public posts
            include_raw_data: Include raw API response in each PostSummary
        """
        # Map user-friendly source names to API values
        api_source = source
        if source == "favorites":
            api_source = "MEDIA_POST_SOURCE_LIKED"

        return await self._browser_client.list_posts(limit, api_source, include_raw_data)

    async def get_post_details(self, post_id: str):
        """Get post details."""
        return await self._browser_client.get_post_details(post_id)

    async def get_asset_file_size(self, asset_url: str) -> int:
        """Get asset file size."""
        return await self._browser_client.get_asset_file_size(asset_url)

    async def validate_auth(self) -> bool:
        """Validate authentication."""
        return await self._browser_client.validate_auth()

    async def match_local_video(self, local_path: str | Path):
        """Match local video to web video."""
        return await self._browser_client.match_local_video(local_path)

    # =========================================================================
    # Favorite APIs - delegate to NodriverClient
    # =========================================================================

    async def favorite_post(self, post_id: str) -> bool:
        """Add a post to favorites.

        Args:
            post_id: Post UUID to favorite

        Returns:
            True if successful
        """
        return await self._browser_client.favorite_post(post_id)

    async def unfavorite_post(self, post_id: str) -> bool:
        """Remove a post from favorites.

        Args:
            post_id: Post UUID to unfavorite

        Returns:
            True if successful
        """
        return await self._browser_client.unfavorite_post(post_id)

    # =========================================================================
    # Social APIs
    # =========================================================================

    async def like_post(self, post_id: str) -> bool:
        """Give a thumbs-up to a post.

        Note: This is different from favorite_post() which saves to favorites.

        Args:
            post_id: Post UUID to like

        Returns:
            True if successful
        """
        return await self._browser_client.like_post(post_id)

    async def dislike_post(self, post_id: str) -> bool:
        """Give a thumbs-down to a post.

        Args:
            post_id: Post UUID to dislike

        Returns:
            True if successful
        """
        return await self._browser_client.dislike_post(post_id)

    # =========================================================================
    # Video APIs
    # =========================================================================

    async def delete_video(self, video_id: str) -> bool:
        """Delete a child video.

        Args:
            video_id: The child video UUID to delete

        Returns:
            True if deletion was successful
        """
        return await self._browser_client.delete_video(video_id)

    async def upgrade_video(self, video_id: str) -> bool:
        """Upgrade a video to HD quality.

        Args:
            video_id: The child video UUID to upgrade

        Returns:
            True if upgrade was initiated successfully
        """
        return await self._browser_client.upgrade_video(video_id)

    async def download_video(
        self,
        video_id: str,
        output_path: str | Path,
        *,
        prefer_hd: bool = True,
        parent_post_id: str | None = None,
    ) -> Path:
        """
        Download a video to local file.

        Args:
            video_id: The video UUID to download. Can be obtained from:
                - PostDetails.children[i].id (from get_post_details)
                - VideoGenerationResult.video_id (from create_video)
                - Web download filename: "{video_id}_hd.mp4" -> video_id is the UUID part
                - Grok URL: https://grok.com/imagine/post/{post_id} (post_id is the parent,
                  use get_post_details to find child video_ids)
            output_path: Destination file path (will be created/overwritten)
            prefer_hd: If True (default), download HD version if available
            parent_post_id: Parent post ID (optional, for faster lookup).
                If provided, skips searching through favorites.

        Returns:
            Path to the downloaded file

        Raises:
            GrokNotFoundError: If video not found in favorites
            GrokAPIError: If download fails

        Note:
            If you already have the video_id, you can construct URLs directly:
            - Web page: https://grok.com/imagine/post/{video_id}
            - HD video: https://imagine-public.x.ai/imagine-public/share-videos/{video_id}_hd.mp4
            - SD video: https://imagine-public.x.ai/imagine-public/share-videos/{video_id}.mp4
            Use NodriverClient.download_video(url, path) for direct download.

        Example:
            >>> # Download a video (auto-detects HD)
            >>> path = await client.download_video(video_id, "output.mp4")
            >>> print(f"Downloaded to {path}")

            >>> # Force standard quality
            >>> path = await client.download_video(video_id, "output.mp4", prefer_hd=False)

            >>> # With parent_post_id for faster lookup
            >>> path = await client.download_video(
            ...     video_id, "output.mp4", parent_post_id=parent_id
            ... )
        """
        output_path = Path(output_path)

        # Find the video URL
        video_url = None

        if parent_post_id:
            # Fast path: we know the parent
            details = await self.get_post_details(parent_post_id)
            for child in details.children:
                if child.id == video_id:
                    video_url = (child.hd_media_url if prefer_hd else None) or child.media_url
                    break
        else:
            # Slow path: search through favorites
            posts = await self.list_posts(limit=100, source="favorites")
            for post in posts:
                details = await self.get_post_details(post.id)
                for child in details.children:
                    if child.id == video_id:
                        video_url = (child.hd_media_url if prefer_hd else None) or child.media_url
                        break
                if video_url:
                    break

        if not video_url:
            raise GrokNotFoundError(f"Video {video_id} not found")

        return await self._browser_client.download_video(video_url, output_path)

    # =========================================================================
    # Image APIs
    # =========================================================================

    async def edit_image(
        self, post_id: str, edit_prompt: str, timeout: int = 60
    ) -> "ImageEditResult":
        """Edit an image to generate new variations.

        Args:
            post_id: The post UUID (parent image)
            edit_prompt: The edit instruction (e.g., "add sunglasses")
            timeout: Max seconds to wait for generation (default 60)

        Returns:
            ImageEditResult with image URLs and moderation info
        """
        return await self._browser_client.edit_image(post_id, edit_prompt, timeout)

    async def create_image(
        self,
        prompt: str,
        aspect_ratio: str = "portrait",
        min_success: int = 1,
        max_scroll: int = 5,
        timeout: int = 120,
        thumbnail_selector: "Callable[[int, Callable[[], Awaitable[list[int]]]], Awaitable[list[int]]] | None" = None,
    ) -> "ImageGenerationResult":
        """
        Generate images from a text prompt (browser only, txt2img).

        This navigates to grok.com/imagine, selects Image mode,
        enters the prompt, and captures generated images. Will scroll
        to generate more images if needed to find non-moderated ones.

        Args:
            prompt: Text description of the image to generate
            aspect_ratio: "portrait" (2:3), "square" (1:1), or "landscape" (3:2)
            min_success: Minimum non-moderated images needed (default 1)
            max_scroll: Maximum scroll attempts to find more images (default 5)
            timeout: Max seconds to wait for initial generation (default 120)
            thumbnail_selector: Optional async callback for selecting which thumbnails
                to collect post_ids for. The callback receives:
                - count: Number of gallery items available
                - scan_favorites: Async function to scan DOM for favorited indices
                The callback should return a list of indices (0-based) to collect.
                Note: Clicking adds ~2 seconds per image for click+back navigation.

        Returns:
            ImageGenerationResult with image URLs and moderation info.
            If thumbnail_selector is provided, selected_post_ids will be populated.

        Example:
            >>> result = await client.create_image("a cat wearing sunglasses")
            >>> print(result.image_urls)  # List of generated image URLs
            >>> print(result.success_count)  # Non-moderated images count
            >>> result.has_enough_success(2)  # Check if got at least 2

            >>> # With automatic selection (select all)
            >>> async def select_all(count, scan_favorites):
            ...     return list(range(count))
            >>> result = await client.create_image("a cat", thumbnail_selector=select_all)
            >>> print(result.selected_post_ids)  # Post IDs for video generation

            >>> # With manual selection (wait for user to favorite)
            >>> async def manual_selector(count, scan_favorites):
            ...     print(f"Please favorite images in browser, then press Enter...")
            ...     await asyncio.get_event_loop().run_in_executor(None, input)
            ...     return await scan_favorites()
            >>> result = await client.create_image("a cat", thumbnail_selector=manual_selector)
        """
        return await self._browser_client.create_image(
            prompt, aspect_ratio, min_success, max_scroll, timeout, thumbnail_selector
        )

    async def upload_image(self, image_path: str | Path, timeout: int = 30) -> str:
        """Upload a local image to Grok Imagine and create a new post.

        Args:
            image_path: Path to the local image file (PNG, JPG, etc.)
            timeout: Max seconds to wait for upload and redirect (default 30)

        Returns:
            The post ID of the newly created post.
        """
        return await self._browser_client.upload_image(image_path, timeout)

    # =========================================================================
    # Unified Video Generation API
    # =========================================================================

    @overload
    async def create_video(
        self,
        prompt: str,
        *,
        aspect_ratio: str = ...,
        timeout: int = ...,
        wait_for_video: bool = ...,
    ) -> VideoGenerationResult:
        """txt2vid: Generate video from text prompt only."""
        ...

    @overload
    async def create_video(
        self,
        prompt: str,
        *,
        source_post_id: str,
        preset: VideoPreset | str = ...,
        aspect_ratio: str = ...,
        timeout: int = ...,
        duration: int = ...,
        resolution: str = ...,
    ) -> VideoGenerationResult:
        """img2vid: Generate video with custom prompt."""
        ...

    @overload
    async def create_video(
        self,
        *,
        source_post_id: str,
        preset: VideoPreset | str,
        aspect_ratio: str = ...,
        timeout: int = ...,
        duration: int = ...,
        resolution: str = ...,
    ) -> VideoGenerationResult:
        """img2vid: Generate video with preset only (no prompt)."""
        ...

    @overload
    async def create_video(
        self,
        prompt: str,
        *,
        source_image_path: str | Path,
        aspect_ratio: str = ...,
        timeout: int = ...,
        duration: int = ...,
        resolution: str = ...,
    ) -> VideoGenerationResult:
        """upload2vid: Upload local image and generate video from it."""
        ...

    async def create_video(
        self,
        prompt: str = "",
        *,
        source_post_id: str | None = None,
        source_image_path: str | Path | None = None,
        preset: VideoPreset | str = "normal",
        aspect_ratio: str = "portrait",
        timeout: int = 300,
        wait_for_video: bool = True,
        duration: int = 10,
        resolution: str = "720p",
    ) -> VideoGenerationResult:
        """
        Unified video generation API supporting multiple modes.

        Args:
            prompt: For txt2vid: full video description.
                   For img2vid/upload2vid: adjustment instructions (camera, motion, style).
            source_post_id: (img2vid) Existing Grok image post ID to animate.
            source_image_path: (upload2vid) Local image path to upload and animate.
            preset: Video style preset - 'normal', 'fun', or 'spicy'.
            aspect_ratio: Video aspect ratio.
            timeout: Max seconds to wait for video generation (default 300).
            wait_for_video: (txt2vid only) Wait for video element to load (default True).
            duration: Video duration in seconds (default 10). Options: 6, 10.
            resolution: Video resolution (default "720p"). Options: "480p", "720p".

        Returns:
            VideoGenerationResult with video_id and metadata.
        """
        return await self._browser_client.create_video(
            prompt=prompt,
            source_post_id=source_post_id,
            source_image_path=source_image_path,
            preset=preset,
            aspect_ratio=aspect_ratio,
            timeout=timeout,
            wait_for_video=wait_for_video,
            duration=duration,
            resolution=resolution,
        )
