"""
Grok Web Connector - API Clients

Two internal client implementations:

AsyncClient
    - Async HTTP client using Playwright
    - Used internally by SmartGrokClient for API requests

NodriverClient
    - Browser automation client using nodriver
    - Used internally by SmartGrokClient for UI operations

Public API:
    Use get_client() from grok_web package - returns SmartGrokClient
    which automatically routes to the appropriate internal client.
"""

from pathlib import Path
from typing import Any, overload

from playwright.async_api import (
    APIRequestContext as AsyncAPIRequestContext,
)
from playwright.async_api import (
    Playwright as AsyncPlaywright,
)
from playwright.async_api import (
    async_playwright,
)

from ._internal import (
    AsyncClientBase,
    build_video_payload,
    generate_statsig_id,
    parse_video_ndjson_response,
    resolve_preset,
)
from .auth import load_config
from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from .models import (
    GrokCookies,
    ImageEditResult,
    ImageGenerationResult,
    VideoGenerationResult,
    VideoPreset,
)

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
# Helper functions
# =============================================================================


def _get_browser_headers() -> dict[str, str]:
    """Get headers that match Playwright's Chromium browser."""
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }


# =============================================================================
# AsyncClient - Async client using Playwright
# =============================================================================


class AsyncClient(AsyncClientBase):
    """
    Async Playwright-based Grok API client.

    For use in async contexts (MCP servers, asyncio apps).
    Must be used as an async context manager.

    Example:
        >>> async with AsyncClient() as client:
        ...     posts = await client.list_posts(limit=10)
    """

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
    ):
        """Initialize (use as async context manager to start Playwright)."""
        super().__init__()  # Initialize business logic layer

        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]

        self.cookies = cookies
        self._cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.to_dict().items()])

        browser_headers = _get_browser_headers()
        browser_headers["Cookie"] = self._cookie_str
        self._headers = browser_headers

        self._playwright: AsyncPlaywright | None = None
        self._api_context: AsyncAPIRequestContext | None = None
        self._asset_context: AsyncAPIRequestContext | None = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._api_context = await self._playwright.request.new_context(
            base_url=self.BASE_URL,
            extra_http_headers=self._headers,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._api_context:
            await self._api_context.dispose()
        if self._asset_context:
            await self._asset_context.dispose()
        if self._playwright:
            await self._playwright.stop()

    async def _get_asset_context(self) -> AsyncAPIRequestContext:
        """Get or create asset context (lazy initialization)."""
        if self._asset_context is None:
            self._asset_context = await self._playwright.request.new_context(
                extra_http_headers={
                    "Origin": "https://grok.com",
                    "Referer": "https://grok.com/",
                    "User-Agent": self._headers["User-Agent"],
                    "Cookie": self._cookie_str,
                }
            )
        return self._asset_context

    # =========================================================================
    # Transport layer (AsyncClientBase abstract methods)
    # =========================================================================

    async def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Grok API."""
        try:
            if method.upper() == "POST":
                response = await self._api_context.post(endpoint, data=json_data)
            elif method.upper() == "GET":
                response = await self._api_context.get(endpoint)
            else:
                raise GrokAPIError(f"Unsupported HTTP method: {method}")
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}") from e

        if response.status in (401, 403):
            text = await response.text()
            if "Just a moment" in text:
                raise GrokAuthError("Cloudflare challenge. Refresh cf_clearance.")
            raise GrokAuthError("Request blocked (401/403). Check cookies.")

        if response.status == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status >= 400:
            raise GrokAPIError(f"API error: {response.status}")

        try:
            return await response.json()
        except ValueError:
            return {}

    async def _api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Make authenticated request and return raw text response."""
        try:
            if method.upper() == "POST":
                response = await self._api_context.post(
                    endpoint, data=json_data, headers=extra_headers, timeout=120000
                )
            elif method.upper() == "GET":
                response = await self._api_context.get(
                    endpoint, headers=extra_headers, timeout=120000
                )
            else:
                raise GrokAPIError(f"Unsupported HTTP method: {method}")
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}") from e

        if response.status in (401, 403):
            text = await response.text()
            if "Just a moment" in text:
                raise GrokAuthError("Cloudflare challenge. Refresh cf_clearance.")
            raise GrokAuthError("Request blocked (401/403). Check cookies.")

        if response.status == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status >= 400:
            raise GrokAPIError(f"API error: {response.status}")

        return await response.text()

    async def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL and return Content-Length."""
        try:
            context = await self._get_asset_context()
            response = await context.head(asset_url)
        except Exception as e:
            raise GrokAPIError(f"Asset request failed: {e}") from e

        if response.status == 403:
            raise GrokAuthError("Asset access denied (403).")

        if response.status != 200:
            raise GrokAPIError(f"Asset request failed: {response.status}")

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header")

        return int(content_length)


# =============================================================================
# NodriverClient - Stealth browser using nodriver (2025 RECOMMENDED)
# =============================================================================


class NodriverClient(AsyncClientBase):
    """
    Stealth browser client using nodriver - RECOMMENDED for 2025.

    Best choice for video generation workflows. Uses Chrome DevTools Protocol
    without WebDriver traces. Automatically handles Cloudflare Turnstile.

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

    # Default port for Chrome remote debugging
    DEFAULT_DEBUG_PORT = 9222
    DEFAULT_DEBUG_HOST = "127.0.0.1"

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
        headless: bool = False,
        host: str | None = None,
        port: int | None = None,
        auto_launch: bool = True,
        ui_delay: float = 1.0,
    ):
        """
        Initialize NodriverClient.

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Defaults to ~/.grok-config.json
            headless: Run browser in headless mode (default: False for debugging)
            host: Remote debugging host. Defaults to "127.0.0.1".
            port: Remote debugging port. Defaults to 9222.
            auto_launch: If True (default), automatically launch Chrome if not running.
                        Set to False to only connect to existing Chrome.
            ui_delay: Multiplier for UI operation delays (default: 1.0).
                     Increase for slower connections, decrease for faster ones.
        """
        super().__init__()  # Initialize business logic layer

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

        # Browser connection settings (always use reuse mode now)
        self._remote_host = host or self.DEFAULT_DEBUG_HOST
        self._remote_port = port or self.DEFAULT_DEBUG_PORT
        self._auto_launch = auto_launch

    async def __aenter__(self):
        import asyncio

        import nodriver
        from nodriver import cdp

        from .browser import ensure_chrome_running, is_port_in_use

        # Ensure Chrome is running (auto-launch if needed)
        if self._auto_launch:
            try:
                self._chrome_process = await ensure_chrome_running(
                    host=self._remote_host,
                    port=self._remote_port,
                    headless=self._headless,
                )
            except FileNotFoundError as e:
                raise GrokAPIError(str(e)) from e
            except TimeoutError as e:
                raise GrokAPIError(f"Chrome failed to start: {e}") from e
        else:
            # Check if Chrome is running when auto_launch is disabled
            if not is_port_in_use(self._remote_host, self._remote_port):
                raise GrokAPIError(
                    f"Chrome not running on {self._remote_host}:{self._remote_port}. "
                    f"Start Chrome with: chrome --remote-debugging-port={self._remote_port} "
                    f"--user-data-dir=/tmp/chrome_debug"
                )

        # Connect to Chrome
        try:
            self._browser = await nodriver.start(
                host=self._remote_host,
                port=self._remote_port,
            )
        except Exception as e:
            raise GrokAPIError(
                f"Failed to connect to Chrome at {self._remote_host}:{self._remote_port}: {e}"
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
        pass

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

        result_str = await self._tab.evaluate(js_code, await_promise=True, return_by_value=True)
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

        result_str = await self._tab.evaluate(js_code, await_promise=True, return_by_value=True)
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
        """Make HEAD request to asset URL via browser fetch."""
        import json as json_module

        js_code = f"""
        (async () => {{
            const resp = await fetch("{asset_url}", {{
                method: "HEAD",
                credentials: "include"
            }});
            return JSON.stringify({{
                status: resp.status,
                contentLength: resp.headers.get("content-length")
            }});
        }})()
        """

        result_str = await self._tab.evaluate(js_code, await_promise=True, return_by_value=True)
        result = json_module.loads(result_str)

        if result["status"] == 403:
            raise GrokAuthError("Asset access denied (403)")
        if result["status"] != 200:
            raise GrokAPIError(f"Asset request failed: {result['status']}")
        if not result["contentLength"]:
            raise GrokAPIError("No Content-Length header")

        return int(result["contentLength"])

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
        video_length: int = 6,
        statsig_id: str | None = None,
        preset: VideoPreset | str = "normal",
        adjustment_prompt: str | None = None,
    ) -> VideoGenerationResult:
        """Generate a video from an image using Grok's chat API.

        NodriverClient override: tries to get statsig_id from page context first.

        Args:
            image_url: Source image URL
            parent_post_id: Parent post UUID
            aspect_ratio: Video aspect ratio (default "2:3")
            video_length: Video duration in seconds (default 6)
            statsig_id: Optional style seed for reproducible styles
            preset: Video preset - 'normal', 'fun', or 'spicy'
            adjustment_prompt: Video generation prompt (same as typing in Grok UI after image).
                Can include any instructions: camera movement, character actions, or both.
                Examples: "Static Shot", "she turns her head", "camera zooms in while he walks".
                If provided, overrides preset and uses 'custom' mode.
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
            if "conversations/new" in url or "app-chat" in url:
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

    async def create_image(
        self,
        prompt: str,
        aspect_ratio: str = "portrait",
        min_success: int = 1,
        max_scroll: int = 5,
        timeout: int = 120,
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

        Returns:
            ImageGenerationResult with job IDs and generation info.
            Note: image_urls and post_ids will be empty since images are temporary.
            Use success_count to check how many images were generated.

        Raises:
            GrokAPIError: If generation fails or times out

        Example:
            >>> result = await client.create_image("a cat wearing sunglasses")
            >>> result.success_count  # Number of completed images
            >>> result.total_count    # Total images generated
        """
        import asyncio
        import json as json_mod

        from nodriver import cdp

        d = self._ui_delay

        # Navigate to imagine page
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

        # Step 5: Click the submit button
        submit_btn = await self._tab.select('button[aria-label="提交"]')
        if submit_btn:
            await submit_btn.mouse_click()
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

        # Step 7: Scroll down to generate more if needed
        # min_success means non-moderated images, so we keep scrolling until we have enough
        scroll_count = 0
        while scroll_count < max_scroll:
            completed = [
                job for job in captured_data["jobs"].values() if job.get("progress") == 100
            ]
            # Count only non-moderated (successful) images
            success_count = sum(1 for job in completed if not job.get("moderated"))

            if success_count >= min_success:
                break

            # Scroll down to trigger more generation
            # The imagine page uses a specific scrollable container, not window
            await self._tab.evaluate("""
                (function() {
                    // Find the scrollable container (has overflow-scroll class)
                    const container = document.querySelector('.overflow-scroll') ||
                                     document.querySelector('[class*="overflow-scroll"]') ||
                                     document.querySelector('main');
                    if (container) {
                        container.scrollTop = container.scrollHeight;
                        return 'scrolled container';
                    }
                    // Fallback to window scroll
                    window.scrollTo(0, document.body.scrollHeight);
                    return 'scrolled window';
                })()
            """)
            await asyncio.sleep(5 * d)  # Wait for new images to generate
            scroll_count += 1

        # Build result
        images = list(captured_data["jobs"].values())

        return ImageGenerationResult(
            prompt=prompt,
            images=images,
            conversation_id=None,  # Not available via WebSocket
        )

    async def create_video_from_text(
        self,
        prompt: str,
        aspect_ratio: str = "portrait",
        timeout: int = 180,
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
            timeout: Max seconds to wait for video generation (default 180)
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
    ) -> VideoGenerationResult:
        """img2vid: Generate video from existing Grok image post."""
        ...

    @overload
    async def create_video(
        self,
        prompt: str,
        *,
        source_image_path: str | Path,
        aspect_ratio: str = ...,
        timeout: int = ...,
    ) -> VideoGenerationResult:
        """upload2vid: Upload local image and generate video from it."""
        ...

    async def create_video(
        self,
        prompt: str,
        *,
        source_post_id: str | None = None,
        source_image_path: str | Path | None = None,
        preset: VideoPreset | str = "normal",
        aspect_ratio: str = "portrait",
        timeout: int = 180,
        wait_for_video: bool = True,
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
            timeout: Max seconds to wait for video generation (default 180).
            wait_for_video: (txt2vid only) Wait for video element to load (default True).

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
            )

        # Mode: img2vid (from existing Grok image post)
        if source_post_id is not None:
            return await self.create_video_via_ui(
                parent_post_id=source_post_id,
                preset=preset,
                timeout=timeout,
                adjustment_prompt=prompt if prompt else None,
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
        timeout: int = 120,
        stable_id: str | None = None,
        adjustment_prompt: str | None = None,
    ) -> VideoGenerationResult:
        """
        Generate video by simulating UI button click (more reliable for anti-bot bypass).

        This navigates to the post page, selects the preset, and clicks "Create Video",
        using the same code path as manual user interaction.

        Args:
            parent_post_id: The image post ID to generate video from
            preset: Video style preset - 'normal', 'fun', or 'spicy' (or VideoPreset enum)
            timeout: Max seconds to wait for video generation
            stable_id: Optional custom stable_id to inject before generation.
                      Use generate_stable_id() to create one. Controls A/B style bucket.
            adjustment_prompt: Video generation prompt (same as typing in Grok UI after image).
                      Can include any instructions: camera movement, character actions, or both.
                      Examples: "Static Shot", "she turns her head", "camera zooms in while he walks".
                      When provided, overrides preset and sets result.mode='custom'.

        Returns:
            VideoGenerationResult with video_id (may be empty if moderated).
            When adjustment_prompt is used, result.mode will be 'custom'.

        Example:
            >>> result = await client.create_video_via_ui("abc-123", preset="fun")
            >>> result.mode  # 'fun'
            >>> result = await client.create_video_via_ui("abc-123", adjustment_prompt="Static Shot")
            >>> result.mode  # 'custom'
            >>> result = await client.create_video_via_ui("abc-123", adjustment_prompt="she smiles")
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
            if "conversations/new" in url or "app-chat" in url:
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

        # Wait for page to fully load (React hydration)
        await asyncio.sleep(3)

        # Scroll down to reveal the "Create Video" button (it's below the image)
        await self._tab.evaluate(
            "window.scrollTo(0, document.body.scrollHeight / 2)", await_promise=False
        )
        await asyncio.sleep(1)

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

        # Non-default preset: selecting the preset auto-generates video
        # Default (Normal): need to click "生成视频" button directly
        preset_selected = False

        # Skip preset selection if using adjustment_prompt (it uses custom mode)
        if adjustment_prompt:
            preset_selected = False  # Force using generate button
        elif preset_str != "normal":
            # Select non-default preset via "视频选项" menu (auto-generates video)
            try:
                buttons = await self._tab.find_all("button, [role='button']")

                # Find and click the "视频选项" (Video Options) button
                video_options_btn = None
                for btn in buttons:
                    label = btn.attrs.get("aria-label", "")
                    if label == "视频选项":
                        video_options_btn = btn
                        break

                if video_options_btn:
                    await video_options_btn.mouse_click()
                    await asyncio.sleep(0.5)

                    # Find and click the preset option in the dropdown menu
                    menu_items = await self._tab.find_all('[role="menuitem"]')
                    for item in menu_items:
                        item_text = item.text.strip() if hasattr(item, "text") else ""
                        # Get text content via JavaScript if needed
                        if not item_text:
                            item_text = await self._tab.evaluate(
                                f"document.querySelectorAll('[role=\"menuitem\"]')[{menu_items.index(item)}].textContent.trim()",
                                await_promise=False,
                            )
                        if item_text == preset_menu_text:
                            await item.mouse_click()
                            preset_selected = True
                            break  # Selecting preset auto-triggers video generation

            except Exception:
                pass  # If preset selection fails, fall back to clicking Create Video

        # For Normal preset (or if preset selection failed), click "Create Video" button
        if not preset_selected:
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
                        or "Create Video" in text
                        or "Make video" in label
                        or label == "生成视频"
                    ):
                        create_btn = btn
                        break

                if not create_btn:
                    raise GrokAPIError("Could not find 'Create Video' button on page")

                await create_btn.mouse_click()

            except Exception as e:
                raise GrokAPIError(f"Failed to click Create Video button: {e}") from e

        # Wait for response with timeout
        start_time = asyncio.get_event_loop().time()
        while captured_response["body"] is None:
            if asyncio.get_event_loop().time() - start_time > timeout:
                raise GrokAPIError("Timeout waiting for video generation response")
            await asyncio.sleep(0.5)

        # Parse response using shared utility (statsig_id captured from request)
        return parse_video_ndjson_response(
            captured_response["body"], parent_post_id, statsig_id=captured_response["statsig_id"]
        )


# =============================================================================
# SmartGrokClient - Recommended client with auto-fallback
# =============================================================================


class SmartGrokClient:
    """
    Recommended client - HTTP for reads, browser fallback for video creation.

    Uses AsyncClient (lightweight HTTP) for all read operations.
    Lazily initializes NodriverClient (browser) only when video creation is needed
    and the API returns 403.

    Example:
        # Start Chrome with remote debugging:
        # chrome --remote-debugging-port=9222

        async with SmartGrokClient(browser_host="127.0.0.1", browser_port=9222) as client:
            posts = await client.list_posts()  # HTTP (fast)
            video = await client.create_video(post_id, preset="fun")  # Browser fallback
    """

    BASE_URL = "https://grok.com"

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
        browser_host: str | None = None,
        browser_port: int | None = None,
        browser_headless: bool = False,
        enable_browser_fallback: bool = True,
    ):
        """
        Initialize SmartGrokClient.

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Defaults to ~/.grok-config.json
            browser_host: Remote debugging host. Defaults to "127.0.0.1".
            browser_port: Remote debugging port. Defaults to 9222.
            browser_headless: Run browser in headless mode (default: False)
            enable_browser_fallback: If True (default), enable browser fallback
                          for video creation. Chrome will be auto-launched if needed.
        """
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]

        self.cookies = cookies
        self._config_path = config_path
        self._http_client: AsyncClient | None = None
        self._browser_client: NodriverClient | None = None
        self._browser_host = browser_host
        self._browser_port = browser_port
        self._browser_headless = browser_headless
        self._enable_browser_fallback = enable_browser_fallback

    async def __aenter__(self):
        """Initialize HTTP client (lightweight, no browser)."""
        self._http_client = AsyncClient(self.cookies)
        await self._http_client.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any):
        """Clean up both clients."""
        if self._http_client:
            await self._http_client.__aexit__(exc_type, exc_val, exc_tb)
        if self._browser_client:
            await self._browser_client.__aexit__(exc_type, exc_val, exc_tb)

    async def _get_browser_client(self) -> NodriverClient:
        """Lazy browser initialization - only when needed."""
        if self._browser_client is None:
            self._browser_client = NodriverClient(
                cookies=self.cookies,
                host=self._browser_host,
                port=self._browser_port,
                headless=self._browser_headless,
            )
            await self._browser_client.__aenter__()
        return self._browser_client

    # =========================================================================
    # Read APIs - HTTP first, browser fallback on Cloudflare challenge
    # =========================================================================

    async def list_posts(
        self, limit: int = 10, source: str | None = "favorites", include_raw_data: bool = False
    ):
        """List posts (HTTP first, browser fallback on Cloudflare).

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

        try:
            return await self._http_client.list_posts(limit, api_source, include_raw_data)
        except GrokAuthError:
            if not self._enable_browser_fallback:
                raise
            browser = await self._get_browser_client()
            return await browser.list_posts(limit, api_source, include_raw_data)

    async def get_post_details(self, post_id: str):
        """Get post details (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.get_post_details(post_id)
        except GrokAuthError:
            if not self._enable_browser_fallback:
                raise
            browser = await self._get_browser_client()
            return await browser.get_post_details(post_id)

    async def get_asset_file_size(self, asset_url: str) -> int:
        """Get asset file size (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.get_asset_file_size(asset_url)
        except GrokAuthError:
            if not self._enable_browser_fallback:
                raise
            browser = await self._get_browser_client()
            return await browser.get_asset_file_size(asset_url)

    async def validate_auth(self) -> bool:
        """Validate authentication (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.validate_auth()
        except GrokAuthError:
            if not self._enable_browser_fallback:
                raise
            browser = await self._get_browser_client()
            return await browser.validate_auth()

    async def match_local_video(self, local_path: str | Path):
        """Match local video to web video (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.match_local_video(local_path)
        except GrokAuthError:
            if not self._enable_browser_fallback:
                raise
            browser = await self._get_browser_client()
            return await browser.match_local_video(local_path)

    # =========================================================================
    # Favorite APIs - HTTP first, browser fallback on Cloudflare
    #
    # Note: The HTTP endpoint /rest/media/post/like is for favorites (Save).
    # API terminology: "like" = "save/favorite" in UI
    # This is different from like_post() which is thumbs-up (赞).
    # =========================================================================

    async def favorite_post(self, post_id: str) -> bool:
        """
        Add a post to favorites (HTTP first, browser fallback on 403).

        Note: API uses "like" terminology for what UI shows as "Save/保存".

        Args:
            post_id: Post UUID to favorite

        Returns:
            True if successful
        """
        try:
            return await self._http_client.favorite_post(post_id)
        except GrokAuthError:
            if not self._enable_browser_fallback:
                raise
            # Use API via browser fetch (not UI clicking)
            # UI method relies on client-side state which may not match server state
            browser = await self._get_browser_client()
            return await browser.favorite_post(post_id)

    async def unfavorite_post(self, post_id: str) -> bool:
        """
        Remove a post from favorites (HTTP first, browser fallback on 403).

        Note: API uses "unlike" terminology for what UI shows as "Unsave/取消保存".

        Args:
            post_id: Post UUID to unfavorite

        Returns:
            True if successful
        """
        try:
            return await self._http_client.unfavorite_post(post_id)
        except GrokAuthError:
            if not self._enable_browser_fallback:
                raise
            # Use API via browser fetch (not UI clicking)
            # UI method relies on client-side state which may not match server state
            browser = await self._get_browser_client()
            return await browser.unfavorite_post(post_id)

    # =========================================================================
    # Social APIs - Browser only (no HTTP API exists)
    # =========================================================================

    async def like_post(self, post_id: str) -> bool:
        """
        Give a thumbs-up to a post (browser only).

        Note: This is different from favorite_post() which saves to favorites.
        This is the "赞" (Like/thumbs up) action.

        Args:
            post_id: Post UUID to like

        Returns:
            True if successful
        """
        browser = await self._get_browser_client()
        return await browser.like_post(post_id)

    async def dislike_post(self, post_id: str) -> bool:
        """
        Give a thumbs-down to a post (browser only).

        Args:
            post_id: Post UUID to dislike

        Returns:
            True if successful
        """
        browser = await self._get_browser_client()
        return await browser.dislike_post(post_id)

    # =========================================================================
    # Video APIs
    # =========================================================================

    async def delete_video(self, video_id: str) -> bool:
        """
        Delete a child video (browser only).

        Args:
            video_id: The child video UUID to delete

        Returns:
            True if deletion was successful
        """
        browser = await self._get_browser_client()
        return await browser.delete_video(video_id)

    async def upgrade_video(self, video_id: str) -> bool:
        """
        Upgrade a video to HD quality (browser only).

        After upgrading, the video's PostDetails will include an `hd_media_url` field
        pointing to the higher quality version (~2x file size). Both URLs remain
        available - use `media_url` for preview, `hd_media_url` for final output.

        This is useful for MCTS workflows: generate many videos at normal quality,
        then upgrade only the selected ones to HD before final export.

        Args:
            video_id: The child video UUID to upgrade

        Returns:
            True if upgrade was initiated successfully

        Example:
            >>> # Check if video has HD, upgrade if not
            >>> post = await client.get_post_details(parent_id)
            >>> for video in post.children:
            ...     if not video.hd_media_url:
            ...         await client.upgrade_video(video.id)
        """
        browser = await self._get_browser_client()
        return await browser.upgrade_video(video_id)

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

        # Add dl=1 parameter to trigger download
        if "?" in video_url:
            download_url = f"{video_url}&dl=1"
        else:
            download_url = f"{video_url}?dl=1"

        # Try HTTP download first
        try:
            context = await self._http_client._get_asset_context()
            response = await context.get(download_url)

            if response.status == 403:
                # Cloudflare blocked, fall back to browser
                raise GrokAuthError("HTTP download blocked (403)")

            if response.status != 200:
                raise GrokAPIError(f"Download failed with status {response.status}")

            # Write to file
            output_path.parent.mkdir(parents=True, exist_ok=True)
            body = await response.body()
            output_path.write_bytes(body)

            return output_path

        except GrokAuthError:
            # Fall back to browser-based download
            browser = await self._get_browser_client()
            return await browser.download_video(video_url, output_path)

    # =========================================================================
    # Image APIs
    # =========================================================================

    async def edit_image(
        self, post_id: str, edit_prompt: str, timeout: int = 60
    ) -> "ImageEditResult":
        """
        Edit an image to generate new variations (browser only).

        Each edit generates 2 images. Some may be moderated (blocked).

        Args:
            post_id: The post UUID (parent image)
            edit_prompt: The edit instruction (e.g., "add sunglasses")
            timeout: Max seconds to wait for generation (default 60)

        Returns:
            ImageEditResult with image URLs and moderation info
        """

        browser = await self._get_browser_client()
        return await browser.edit_image(post_id, edit_prompt, timeout)

    async def create_image(
        self,
        prompt: str,
        aspect_ratio: str = "portrait",
        min_success: int = 1,
        max_scroll: int = 5,
        timeout: int = 120,
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

        Returns:
            ImageGenerationResult with image URLs and moderation info

        Example:
            >>> result = await client.create_image("a cat wearing sunglasses")
            >>> print(result.image_urls)  # List of generated image URLs
            >>> print(result.success_count)  # Non-moderated images count
            >>> result.has_enough_success(2)  # Check if got at least 2
        """
        browser = await self._get_browser_client()
        return await browser.create_image(prompt, aspect_ratio, min_success, max_scroll, timeout)

    async def upload_image(self, image_path: str | Path, timeout: int = 30) -> str:
        """
        Upload a local image to Grok Imagine and create a new post.

        The uploaded image can then be used for video generation or editing.

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
            >>> # Generate video from uploaded image
            >>> video = await client.create_video("zoom in", source_post_id=post_id)
            >>> # Or edit the uploaded image
            >>> result = await client.edit_image(post_id, "add sunglasses")
        """
        browser = await self._get_browser_client()
        return await browser.upload_image(image_path, timeout)

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
    ) -> VideoGenerationResult:
        """img2vid: Generate video from existing Grok image post."""
        ...

    @overload
    async def create_video(
        self,
        prompt: str,
        *,
        source_image_path: str | Path,
        aspect_ratio: str = ...,
        timeout: int = ...,
    ) -> VideoGenerationResult:
        """upload2vid: Upload local image and generate video from it."""
        ...

    async def create_video(
        self,
        prompt: str,
        *,
        source_post_id: str | None = None,
        source_image_path: str | Path | None = None,
        preset: VideoPreset | str = "normal",
        aspect_ratio: str = "portrait",
        timeout: int = 180,
        wait_for_video: bool = True,
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
                   Only used for img2vid mode. Ignored if prompt contains custom instructions.
            aspect_ratio: Video aspect ratio.
                         - txt2vid: "portrait" (9:16), "square" (1:1), "landscape" (16:9)
                         - img2vid: "2:3", "1:1", "3:2"
            timeout: Max seconds to wait for video generation (default 180).
            wait_for_video: (txt2vid only) Wait for video element to load (default True).

        Returns:
            VideoGenerationResult with video_id and metadata.

        Raises:
            ValueError: If both source_post_id and source_image_path are provided.
            FileNotFoundError: If source_image_path file doesn't exist.
            GrokAuthError: If API is blocked and browser fallback is disabled.

        Examples:
            # txt2vid - Generate video from text description
            >>> video = await client.create_video("a cat playing with yarn")

            # img2vid - Animate existing image with preset
            >>> video = await client.create_video(
            ...     "zoom in slowly",
            ...     source_post_id="abc-123-def",
            ...     preset="fun"
            ... )

            # img2vid - Animate with custom camera/motion instructions
            >>> video = await client.create_video(
            ...     "she turns her head, Pan Left, cinematic lighting",
            ...     source_post_id="abc-123-def"
            ... )

            # upload2vid - Upload local image and animate (NOT YET IMPLEMENTED)
            >>> video = await client.create_video(
            ...     "make him smile",
            ...     source_image_path="/path/to/photo.jpg"
            ... )
        """
        # Validate parameters
        if source_post_id is not None and source_image_path is not None:
            raise ValueError("Cannot specify both source_post_id and source_image_path.")

        # upload2vid requires browser (no HTTP API)
        if source_image_path is not None:
            browser = await self._get_browser_client()
            return await browser.create_video(
                prompt=prompt,
                source_image_path=source_image_path,
                preset=preset,
                aspect_ratio=aspect_ratio,
                timeout=timeout,
            )

        # For img2vid with non-custom preset, try HTTP API first
        if source_post_id is not None and (not prompt or preset != "normal"):
            try:
                post = await self.get_post_details(source_post_id)
                return await self._http_client.create_video_from_image(
                    image_url=post.media_url,
                    parent_post_id=source_post_id,
                    preset=preset,
                    aspect_ratio=aspect_ratio,
                    video_length=6,
                )
            except GrokAuthError:
                if not self._enable_browser_fallback:
                    raise GrokAuthError(
                        "Video API blocked (403). Enable browser fallback or use browser client."
                    ) from None
                # Fall through to browser

        # Delegate to NodriverClient.create_video (single source of truth)
        browser = await self._get_browser_client()
        return await browser.create_video(
            prompt=prompt,
            source_post_id=source_post_id,
            source_image_path=source_image_path,
            preset=preset,
            aspect_ratio=aspect_ratio,
            timeout=timeout,
            wait_for_video=wait_for_video,
        )
