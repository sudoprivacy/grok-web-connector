"""
Grok Web Connector - API Clients

Three client implementations for different use cases:

GrokClient (recommended)
    - Uses curl_cffi with Chrome TLS fingerprint impersonation
    - Best for macOS/Linux
    - Lightweight, fast startup

PlaywrightClient
    - Uses Playwright's native Chromium TLS
    - Reliable Cloudflare bypass on all platforms
    - Use when GrokClient fails with 403 errors
    - Must be used as context manager: with PlaywrightClient() as client:

AsyncClient
    - Async version using Playwright
    - For async contexts (MCP servers, asyncio applications)
    - Must be used as async context manager: async with AsyncClient() as client:
"""

from pathlib import Path
from typing import Any

from curl_cffi import requests
from playwright.async_api import (
    APIRequestContext as AsyncAPIRequestContext,
)
from playwright.async_api import (
    Playwright as AsyncPlaywright,
)
from playwright.async_api import (
    async_playwright,
)
from playwright.sync_api import APIRequestContext, Playwright, sync_playwright

from ._internal import (
    AsyncClientBase,
    SyncClientBase,
    build_video_payload,
    generate_statsig_id,
    parse_video_ndjson_response,
    resolve_preset,
)
from .auth import DEFAULT_IMPERSONATE, get_platform_headers, load_config
from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from .models import GrokCookies, VideoGenerationResult, VideoPreset

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
# GrokClient - Default client using curl_cffi
# =============================================================================


class GrokClient(SyncClientBase):
    """
    Default Grok API client using curl_cffi.

    Uses Chrome TLS fingerprint impersonation to bypass Cloudflare.
    Best for macOS/Linux. If you get 403 errors, try PlaywrightClient.

    Example:
        >>> client = GrokClient()
        >>> posts = client.list_posts(limit=10)
        >>> details = client.get_post_details(posts[0].id)
    """

    # x-statsig-id is required for chat API (create_video_from_image)
    # This appears to be a Statsig SDK client ID, reusable across requests
    DEFAULT_STATSIG_ID = (
        "W6IFgVSv2YSVxFj5Yt971KvAL1ldD75XJoGIR285iLdGPIiPNM7S1C9An8vmKsYb"
        "R9N5sF963w2iXoRhwSHYizPczaEUWA"
    )

    BASE_API_HEADERS = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "sec-ch-ua-mobile": "?0",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-statsig-id": DEFAULT_STATSIG_ID,
    }

    BASE_ASSET_HEADERS = {
        "referer": "https://grok.com/",
        "origin": "https://grok.com",
    }

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
    ):
        """
        Initialize Grok client.

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Defaults to ~/.grok-config.json
        """
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]
            custom_headers = config["headers"]
            impersonate = config["impersonate"]
        else:
            custom_headers = {}
            impersonate = DEFAULT_IMPERSONATE

        self.cookies = cookies
        self._impersonate = impersonate

        platform_headers = get_platform_headers()

        self._api_headers = {
            **self.BASE_API_HEADERS,
            **platform_headers,
            **custom_headers,
        }

        self._asset_headers = {
            **self.BASE_ASSET_HEADERS,
            "user-agent": platform_headers["user-agent"],
            **{k: v for k, v in custom_headers.items() if k == "user-agent"},
        }

        self._session = requests.Session(impersonate=impersonate)
        self._session.headers.update(self._api_headers)
        self._session.cookies.update(cookies.to_dict())

    def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Grok API."""
        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = self._session.request(method, url, json=json_data)
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}") from e

        if response.status_code in (401, 403):
            raise GrokAuthError(
                "Request blocked (401/403). Try PlaywrightClient instead, "
                "or update ~/.grok-config.json cookies."
            )

        if response.status_code == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status_code >= 400:
            raise GrokAPIError(f"API error: {response.status_code}")

        try:
            return response.json()
        except ValueError:
            return {}

    def _api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Make authenticated request and return raw text response.

        Used for streaming endpoints that return NDJSON (like create_video_from_image).

        Args:
            extra_headers: Additional headers to include (overrides session headers)
        """
        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = self._session.request(
                method, url, json=json_data, headers=extra_headers, timeout=120
            )
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}") from e

        if response.status_code in (401, 403):
            raise GrokAuthError(
                "Request blocked (401/403). Try PlaywrightClient instead, "
                "or update ~/.grok-config.json cookies."
            )

        if response.status_code == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status_code >= 400:
            raise GrokAPIError(f"API error: {response.status_code}")

        return response.text

    def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL and return Content-Length."""
        try:
            response = requests.head(
                asset_url,
                headers=self._asset_headers,
                cookies=self.cookies.to_dict(),
                timeout=15,
                impersonate=self._impersonate,
            )
        except Exception as e:
            raise GrokAPIError(f"Asset request failed: {e}") from e

        if response.status_code == 403:
            raise GrokAuthError("Asset access denied (403). Try PlaywrightClient.")

        if response.status_code != 200:
            raise GrokAPIError(f"Asset request failed: {response.status_code}")

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header in response")

        return int(content_length)


# =============================================================================
# PlaywrightClient - Sync client using Playwright
# =============================================================================


class PlaywrightClient(SyncClientBase):
    """
    Playwright-based Grok API client (sync).

    Uses native Chromium TLS for reliable Cloudflare bypass.
    Works on all platforms. Must be used as a context manager.

    Example:
        >>> with PlaywrightClient() as client:
        ...     posts = client.list_posts(limit=10)
    """

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
    ):
        """Initialize (use as context manager to start Playwright)."""
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]

        self.cookies = cookies
        self._cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.to_dict().items()])

        browser_headers = _get_browser_headers()
        browser_headers["Cookie"] = self._cookie_str
        self._headers = browser_headers

        self._playwright: Playwright | None = None
        self._api_context: APIRequestContext | None = None
        self._asset_context: APIRequestContext | None = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        self._api_context = self._playwright.request.new_context(
            base_url=self.BASE_URL,
            extra_http_headers=self._headers,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._api_context:
            self._api_context.dispose()
        if self._asset_context:
            self._asset_context.dispose()
        if self._playwright:
            self._playwright.stop()

    def _get_asset_context(self) -> APIRequestContext:
        """Get or create asset context (lazy initialization)."""
        if self._asset_context is None:
            self._asset_context = self._playwright.request.new_context(
                extra_http_headers={
                    "Origin": "https://grok.com",
                    "Referer": "https://grok.com/",
                    "User-Agent": self._headers["User-Agent"],
                    "Cookie": self._cookie_str,
                }
            )
        return self._asset_context

    def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request to Grok API."""
        try:
            if method.upper() == "POST":
                response = self._api_context.post(endpoint, data=json_data)
            elif method.upper() == "GET":
                response = self._api_context.get(endpoint)
            else:
                raise GrokAPIError(f"Unsupported HTTP method: {method}")
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}") from e

        if response.status in (401, 403):
            text = response.text()
            if "Just a moment" in text:
                raise GrokAuthError("Cloudflare challenge detected. Refresh cf_clearance cookie.")
            raise GrokAuthError("Request blocked (401/403). Check cookies.")

        if response.status == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status >= 400:
            raise GrokAPIError(f"API error: {response.status}")

        try:
            return response.json()
        except ValueError:
            return {}

    def _api_request_text(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Make authenticated request and return raw text response.

        Args:
            extra_headers: Additional headers to include (e.g., x-statsig-id)
        """
        try:
            if method.upper() == "POST":
                response = self._api_context.post(
                    endpoint, data=json_data, headers=extra_headers, timeout=120000
                )
            elif method.upper() == "GET":
                response = self._api_context.get(endpoint, headers=extra_headers, timeout=120000)
            else:
                raise GrokAPIError(f"Unsupported HTTP method: {method}")
        except Exception as e:
            raise GrokAPIError(f"Request failed: {e}") from e

        if response.status in (401, 403):
            text = response.text()
            if "Just a moment" in text:
                raise GrokAuthError("Cloudflare challenge detected. Refresh cf_clearance cookie.")
            raise GrokAuthError("Request blocked (401/403). Check cookies.")

        if response.status == 404:
            raise GrokNotFoundError("Resource not found")

        if response.status >= 400:
            raise GrokAPIError(f"API error: {response.status}")

        return response.text()

    def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL and return Content-Length."""
        try:
            context = self._get_asset_context()
            response = context.head(asset_url)
        except Exception as e:
            raise GrokAPIError(f"Asset request failed: {e}") from e

        if response.status == 403:
            raise GrokAuthError("Asset access denied (403). Refresh cf_clearance.")

        if response.status != 200:
            raise GrokAPIError(f"Asset request failed: {response.status}")

        content_length = response.headers.get("content-length")
        if not content_length:
            raise GrokAPIError("No Content-Length header in response")

        return int(content_length)


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

    RECOMMENDED: Use with persistent Chrome for fastest performance.

    Setup (once):
        # macOS:
        /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
            --remote-debugging-port=9222

        # Windows:
        chrome.exe --remote-debugging-port=9222

        # Linux:
        google-chrome --remote-debugging-port=9222

    Usage (fast after first connection):
        >>> # Connect to persistent Chrome (RECOMMENDED)
        >>> async with NodriverClient(host="127.0.0.1", port=9222) as client:
        ...     # First request: ~5s (Cloudflare challenge)
        ...     # Subsequent: instant (session reused)
        ...     posts = await client.list_posts(limit=10)
        ...     result = await client.create_video_from_image(...)

        >>> # Or use get_client() factory (same thing)
        >>> from grok_web import get_client
        >>> async with get_client(host="127.0.0.1", port=9222) as client:
        ...     posts = await client.list_posts()

    Without persistent Chrome (slower, starts new browser each time):
        >>> async with NodriverClient() as client:
        ...     posts = await client.list_posts(limit=10)

    Why persistent Chrome?
        - First startup: ~5s (launches Chrome, handles Cloudflare)
        - Subsequent calls: instant (reuses browser session)
        - Browser stays open between script runs
        - Perfect for batch video generation
    """

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
        headless: bool = False,
        host: str | None = None,
        port: int | None = None,
    ):
        """
        Initialize NodriverClient.

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Defaults to ~/.grok-config.json
            headless: Run browser in headless mode (default: False for debugging)
            host: Remote debugging host (e.g., "127.0.0.1"). If set with port,
                  connects to existing Chrome instead of starting new one.
            port: Remote debugging port (e.g., 9222). Requires host to be set.
        """
        if cookies is None:
            config = load_config(config_path)
            cookies = config["cookies"]

        self.cookies = cookies
        self._headless = headless
        self._browser = None
        self._tab = None
        self._initialized = False

        # Browser reuse support
        self._remote_host = host
        self._remote_port = port
        self._browser_reuse = host is not None and port is not None

    async def __aenter__(self):
        import asyncio

        import nodriver
        from nodriver import cdp

        # Connect to existing browser or start new one
        if self._browser_reuse:
            self._browser = await nodriver.start(
                host=self._remote_host,
                port=self._remote_port,
            )
        else:
            self._browser = await nodriver.start(headless=self._headless)

        # Get a tab first (blank page)
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
        if self._browser and not self._browser_reuse:
            # Only stop browser if we started it (not when reusing existing)
            self._browser.stop()

    async def _api_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """Make authenticated request via browser fetch."""
        import json as json_module

        url = f"{self.BASE_URL}{endpoint}"
        payload_str = json_module.dumps(json_data) if json_data else "null"

        # Escape the payload for embedding in JS string
        payload_escaped = payload_str.replace("\\", "\\\\").replace("'", "\\'")

        js_code = f"""
        (async () => {{
            const resp = await fetch("{url}", {{
                method: "{method.upper()}",
                headers: {{"Content-Type": "application/json"}},
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
    ) -> VideoGenerationResult:
        """Generate a video from an image using Grok's chat API.

        NodriverClient override: tries to get statsig_id from page context first.
        """
        # Try to get statsig_id from page context first, then generate if not found
        if statsig_id is None:
            statsig_id = await self._get_statsig_id_from_page()
        if statsig_id is None:
            statsig_id = generate_statsig_id()

        # Build payload using shared utilities
        mode_value = resolve_preset(preset)
        payload = build_video_payload(
            image_url, parent_post_id, mode_value, aspect_ratio, video_length
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

    async def create_video_via_ui(
        self,
        parent_post_id: str,
        preset: VideoPreset | str = VideoPreset.NORMAL,
        timeout: int = 120,
    ) -> VideoGenerationResult:
        """
        Generate video by simulating UI button click (more reliable for anti-bot bypass).

        This navigates to the post page, selects the preset, and clicks "Create Video",
        using the same code path as manual user interaction.

        Args:
            parent_post_id: The image post ID to generate video from
            preset: Video style preset - 'normal', 'fun', or 'spicy' (or VideoPreset enum)
            timeout: Max seconds to wait for video generation

        Returns:
            VideoGenerationResult with video_id (may be empty if moderated)

        Example:
            >>> result = await client.create_video_via_ui(
            ...     parent_post_id="abc-123",
            ...     preset="fun",  # or VideoPreset.FUN
            ... )
        """
        import asyncio

        from nodriver import cdp

        # Normalize preset to string
        preset_str = preset.value if isinstance(preset, VideoPreset) else str(preset).lower()

        # Map preset string to menu text (case-sensitive as shown in UI)
        preset_menu_map = {
            "normal": "Normal",
            "fun": "Fun",
            "spicy": "Spicy",
        }
        preset_menu_text = preset_menu_map.get(preset_str, "Normal")

        # Navigate to the post page
        await self._navigate_to_post(parent_post_id)

        # Set up network monitoring to capture the response
        await self._tab.send(cdp.network.enable())

        captured_response = {"body": None, "request_id": None}

        async def handle_request(event: cdp.network.RequestWillBeSent):
            url = event.request.url
            if "conversations/new" in url or "app-chat" in url:
                captured_response["request_id"] = event.request_id

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

        # Non-default preset: selecting the preset auto-generates video
        # Default (Normal): need to click "生成视频" button directly
        preset_selected = False

        if preset_str != "normal":
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

                    if "生成视频" in text or "Create Video" in text or label == "生成视频":
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

        # Parse response using shared utility (statsig_id unknown from UI click)
        return parse_video_ndjson_response(
            captured_response["body"], parent_post_id, statsig_id=None
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
    ):
        """
        Initialize SmartGrokClient.

        Args:
            cookies: GrokCookies instance. If None, loads from config file.
            config_path: Path to config file. Defaults to ~/.grok-config.json
            browser_host: Remote debugging host for Chrome (e.g., "127.0.0.1").
                          Required for video creation fallback.
            browser_port: Remote debugging port for Chrome (e.g., 9222).
                          Required for video creation fallback.
            browser_headless: Run browser in headless mode (default: False)
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
        self, limit: int = 10, source: str = "favorites", include_raw_data: bool = False
    ):
        """List posts (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.list_posts(limit, source, include_raw_data)
        except GrokAuthError:
            if self._browser_host is None or self._browser_port is None:
                raise
            browser = await self._get_browser_client()
            return await browser.list_posts(limit, source, include_raw_data)

    async def get_post_details(self, post_id: str):
        """Get post details (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.get_post_details(post_id)
        except GrokAuthError:
            if self._browser_host is None or self._browser_port is None:
                raise
            browser = await self._get_browser_client()
            return await browser.get_post_details(post_id)

    async def get_asset_file_size(self, asset_url: str) -> int:
        """Get asset file size (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.get_asset_file_size(asset_url)
        except GrokAuthError:
            if self._browser_host is None or self._browser_port is None:
                raise
            browser = await self._get_browser_client()
            return await browser.get_asset_file_size(asset_url)

    async def validate_auth(self) -> bool:
        """Validate authentication (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.validate_auth()
        except GrokAuthError:
            if self._browser_host is None or self._browser_port is None:
                raise
            browser = await self._get_browser_client()
            return await browser.validate_auth()

    async def match_local_video(self, local_path: str | Path):
        """Match local video to web video (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.match_local_video(local_path)
        except GrokAuthError:
            if self._browser_host is None or self._browser_port is None:
                raise
            browser = await self._get_browser_client()
            return await browser.match_local_video(local_path)

    # =========================================================================
    # Write APIs - HTTP first, browser fallback on Cloudflare
    # =========================================================================

    async def like_post(self, post_id: str) -> bool:
        """Like a post (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.like_post(post_id)
        except GrokAuthError:
            if self._browser_host is None or self._browser_port is None:
                raise
            browser = await self._get_browser_client()
            return await browser.like_post(post_id)

    async def unlike_post(self, post_id: str) -> bool:
        """Unlike a post (HTTP first, browser fallback on Cloudflare)."""
        try:
            return await self._http_client.unlike_post(post_id)
        except GrokAuthError:
            if self._browser_host is None or self._browser_port is None:
                raise
            browser = await self._get_browser_client()
            return await browser.unlike_post(post_id)

    async def create_video(
        self,
        parent_post_id: str,
        image_url: str | None = None,
        preset: VideoPreset | str = "normal",
        aspect_ratio: str = "2:3",
        video_length: int = 6,
    ) -> VideoGenerationResult:
        """
        Create video with auto-fallback: try HTTP first, browser if blocked.

        Args:
            parent_post_id: Image post ID to generate video from
            image_url: Optional image URL (fetched from post if not provided)
            preset: Video style - 'normal', 'fun', or 'spicy'
            aspect_ratio: Video aspect ratio (default "2:3")
            video_length: Video length in seconds (default 6)

        Returns:
            VideoGenerationResult with video_id

        Note:
            If HTTP API returns 403, automatically falls back to browser UI.
            Browser fallback requires browser_host and browser_port to be set.
        """
        # Get image URL if not provided
        if image_url is None:
            post = await self.get_post_details(parent_post_id)
            image_url = post.media_url

        # Try HTTP API first
        try:
            return await self._http_client.create_video_from_image(
                image_url=image_url,
                parent_post_id=parent_post_id,
                preset=preset,
                aspect_ratio=aspect_ratio,
                video_length=video_length,
            )
        except GrokAuthError:
            # API blocked (403) - fallback to browser UI
            if self._browser_host is None or self._browser_port is None:
                raise GrokAuthError(
                    "Video API blocked (403). Browser fallback requires browser_host and browser_port. "
                    "Start Chrome with: chrome --remote-debugging-port=9222"
                ) from None
            browser = await self._get_browser_client()
            return await browser.create_video_via_ui(parent_post_id, preset=preset)
