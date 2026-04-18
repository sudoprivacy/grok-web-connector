"""
Grok Web Connector - GrokClient

Browser automation client using Chrome DevTools Protocol (via ai-dev-browser/CDP).
Handles all Grok API operations: reads, writes, video/image generation, and UI automation.

Public API:
    Use get_client() from grok_web package — returns GrokClient.
"""

import logging
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ai_dev_browser.core.config import DEFAULT_DEBUG_HOST

from ._internal import (
    MEDIA_POST_GET_ENDPOINT,
    MEDIA_POST_LIKE_ENDPOINT,
    MEDIA_POST_LIST_ENDPOINT,
    MEDIA_POST_UNLIKE_ENDPOINT,
    ResponseParser,
    parse_video_ndjson_response,
)
from .auth import DEFAULT_CONFIG_PATH, load_config, save_cookies
from .exceptions import GrokAPIError, GrokAuthError, GrokNotFoundError
from .models import (
    MODE_TXT2VID,
    GrokCookies,
    ImageEditResult,
    ImageGenerationResult,
    ImageVideoMapping,
    PostDetails,
    PostSummary,
    VideoExtendResult,
    VideoGenerationResult,
    VideoMatchResult,
)

# =============================================================================
# Logging
# =============================================================================

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

# Named profile for grok Chrome (persistent across runs)
GROK_CHROME_PROFILE = "grok-chrome"

# Grok returns this fixed thumbnail image UUID when a post is moderated
# (hidden-from-view, shown in UI as a slashed-eye icon). Observed directly
# via /rest/media/post/get on moderated videos, both for immediate and
# post-render moderation.
MODERATED_THUMBNAIL_UUID = "21d8b635-e385-4cff-8faf-6716975dbd2a"

# x-statsig-id is required for Grok API requests
# This is a Statsig SDK client ID, reusable across requests
DEFAULT_STATSIG_ID = (
    "W6IFgVSv2YSVxFj5Yt971KvAL1ldD75XJoGIR285iLdGPIiPNM7S1C9An8vmKsYbR9N5sF963w2iXoRhwSHYizPczaEUWA"
)


# =============================================================================
# GrokClient - Browser automation via ai-dev-browser/CDP
# =============================================================================


class GrokClient(ResponseParser):
    """
    Grok Imagine browser automation client.

    Uses Chrome DevTools Protocol (via ai-dev-browser) for all
    Grok API operations. Automatically handles cookie loading, interactive
    login setup, Chrome lifecycle, and Cloudflare Turnstile.

    Usage:
        >>> async with GrokClient() as client:
        ...     posts = await client.list_posts(limit=10)
        ...     result = await client.create_video({"images": ["post:" + post_id]})

        >>> # Or use get_client() factory
        >>> from grok_web import get_client
        >>> async with get_client() as client:
        ...     posts = await client.list_posts()

    Performance:
        - First run: ~5s (launches Chrome, handles Cloudflare)
        - Subsequent runs: instant (reuses browser session)
        - Chrome stays open between script runs for fast batch processing
    """

    BASE_URL = "https://grok.com"

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
        Initialize GrokClient.

        Args:
            cookies: GrokCookies instance. If None, loads from config (with
                    interactive setup if config is missing).
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
        # Store for deferred loading in __aenter__
        self._provided_cookies = cookies
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

        self.cookies: GrokCookies | None = None
        self._headless = headless
        self._browser = None
        self._tab = None
        self._initialized = False
        self._chrome_process = None  # Track Chrome process we launched
        self._ui_delay = ui_delay

        # Snitch for x-statsig-id (populated passively from any grok.com request
        # seen on this tab). Used by direct REST submit to pass anti-bot check.
        self._statsig_snitch = None

        # Browser connection settings
        self._remote_host = host or DEFAULT_DEBUG_HOST
        self._remote_port = port  # None = let start_browser auto-assign
        self._auto_launch = auto_launch
        self._force_new_chrome = force_new_chrome
        self._profile = profile

    async def _load_or_setup_cookies(self) -> GrokCookies:
        """Load cookies from config, or trigger interactive setup if missing."""
        from .exceptions import GrokConfigError

        try:
            config = load_config(self._config_path)
            return config["cookies"]
        except GrokConfigError:
            print("No valid Grok cookies found. Starting interactive login...")
            from .auth_manager import AuthManager

            auth = AuthManager(config_path=self._config_path)
            success = await auth.setup_auth(timeout_minutes=5, headless=False)

            if not success:
                raise GrokConfigError(
                    "Authentication setup failed.\n"
                    "Please run: python -m grok_web.auth_manager setup"
                ) from None

            config = load_config(self._config_path)
            return config["cookies"]

    async def __aenter__(self):
        import asyncio

        from ai_dev_browser import cdp
        from ai_dev_browser.core import browser_start
        from ai_dev_browser.core.connection import connect_browser

        # Load cookies (deferred from __init__)
        if self._provided_cookies is not None:
            self.cookies = self._provided_cookies
        else:
            self.cookies = await self._load_or_setup_cookies()

        # Ensure Chrome is running (auto-launch if needed)
        actual_port = self._remote_port  # Default to requested port
        if self._auto_launch:
            try:
                profile_name = self._profile or GROK_CHROME_PROFILE
                kwargs = {"headless": self._headless, "profile": profile_name}
                if self._remote_port is not None:
                    kwargs["port"] = self._remote_port
                if self._force_new_chrome:
                    kwargs["reuse"] = "none"

                result = browser_start(**kwargs)
                if "error" in result:
                    raise RuntimeError(result["error"])

                actual_port = result["port"]
                if result.get("reused"):
                    logger.info(f"Reusing Chrome on port {actual_port} (profile: {profile_name})")
                else:
                    logger.info(
                        f"Started new Chrome on port {actual_port} (profile: {profile_name})"
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
        from ai_dev_browser.core.cloudflare import cloudflare_verify

        result = await cloudflare_verify(self._tab, max_retries=15)
        if not result.get("verified"):
            raise GrokAuthError("Failed to bypass Cloudflare challenge")

        # Install passive x-statsig-id snitch. The frontend rotates this
        # header on every outbound API call; we cache the latest for
        # direct REST submits (e.g. create_video with file: references).
        from .actions.direct_rest import StatsigSnitch

        self._statsig_snitch = StatsigSnitch(self._tab)
        await self._statsig_snitch.install()

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
                f"Browser evaluation failed after recovery. Received: {type(result).__name__}."
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

    async def _asset_request_head(self, asset_url: str) -> int:
        """Make HEAD request to asset URL via CDP Network.loadNetworkResource.

        This bypasses JavaScript fetch CORS restrictions by using Chrome
        DevTools Protocol directly. Works reliably on both Windows and
        macOS.

        CRITICAL: ``Network.loadNetworkResource`` hands back an ``IO``
        stream handle pointing at the response body. The caller is
        expected to release it via ``IO.close`` — we don't read the body
        (we only care about the Content-Length header), so the stream
        sits unclosed otherwise. In a BrowserWorkerPool the leak
        compounds: ``match_local_video`` issues dozens of these per
        favorite scanned, and after a few hundred unreleased handles
        Chrome starts refusing new CDP WebSocket upgrades with HTTP 500.
        The fix is a ``finally`` that always closes the stream, even on
        error paths.
        """
        from ai_dev_browser import cdp

        stream_handle = None
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

            # Remember the stream handle (if Chrome gave one) so we can
            # release it in `finally` regardless of which branch returns
            # or raises below.
            stream_handle = getattr(response, "stream", None)

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
                f"CDP network request failed for asset. URL: {asset_url}, Error: {e}"
            ) from e
        finally:
            # Release the stream handle even if we bailed early — we
            # never read the body, Chrome has nothing to wait for.
            if stream_handle is not None:
                try:
                    await self._tab.send(cdp.io.close(handle=stream_handle))
                except Exception as close_err:  # noqa: BLE001
                    # Closing is best-effort. Log quietly — the main
                    # work already succeeded or failed with its own
                    # error.
                    logger.debug(
                        "Failed to close CDP stream handle for %s: %s",
                        asset_url,
                        close_err,
                    )

    # =========================================================================
    # API Methods (business logic using the I/O primitives above)
    # =========================================================================

    async def list_posts(
        self,
        limit: int | None = 40,
        source: str | None = "favorites",
        include_raw_data: bool = False,
    ) -> list[PostSummary]:
        """List posts with basic metadata, with automatic pagination.

        Args:
            limit: Maximum number of posts to return, or None for all.
                Pagination is handled automatically via cursor.
            source: Filter by source type:
                - "favorites": Your saved/favorited posts (default)
                - None: All public posts
            include_raw_data: Include raw API response in each PostSummary
        """
        # Map user-friendly source names to API values
        api_source = source
        if source == "favorites":
            api_source = "MEDIA_POST_SOURCE_LIKED"

        filter_data: dict[str, Any] = {"source": api_source} if api_source else {}

        posts: list[PostSummary] = []
        cursor: str | None = None

        while True:
            page_limit = 2000 if limit is None else min(limit - len(posts), 2000)
            json_data: dict[str, Any] = {"limit": page_limit, "filter": filter_data}
            if cursor:
                json_data["cursor"] = cursor

            data = await self._api_request("POST", MEDIA_POST_LIST_ENDPOINT, json_data)

            page_posts = data.get("posts", [])
            if not page_posts:
                break

            for item in page_posts:
                try:
                    summary = self._parse_post_summary(item, include_raw_data=include_raw_data)
                    posts.append(summary)
                except Exception:
                    continue

            if limit is not None and len(posts) >= limit:
                break

            cursor = data.get("nextCursor", "")
            if not cursor:
                break

        return posts

    async def get_post_details(self, post_id: str) -> PostDetails:
        """Get full details of a post including all child videos."""
        data = await self._api_request("POST", MEDIA_POST_GET_ENDPOINT, {"id": post_id})
        post_data = data.get("post", data)
        return self._parse_post_details(post_data, post_id, raw_data=data)

    async def check_video_moderated(self, video_id: str) -> bool:
        """Check whether a generated video was moderated by Grok.

        Unlike the ``moderated`` field on ``VideoGenerationResult`` — which
        only reflects the immediate NDJSON response from the generation
        endpoint — this consults ``/rest/media/post/get`` which is updated
        with the post-render moderation verdict. Use this after
        ``create_video()`` to catch videos that *passed* initial prompt/ref
        moderation but were blocked by post-render content review.

        Detection signals (any one → moderated):

        - ``mediaUrl`` is empty on the post or its first child video
          (non-moderated finished videos always have a populated URL)
        - ``thumbnailImageUrl`` contains the fixed moderated-placeholder
          image UUID (``MODERATED_THUMBNAIL_UUID``)

        Args:
            video_id: UUID returned by ``create_video()`` (or equivalently
                ``VideoGenerationResult.video_id``).

        Returns:
            True if Grok moderated the final video, False otherwise.

        Example:
            >>> result = await client.create_video({"images": paths, "prompt": "..."})
            >>> if not result.moderated and await client.check_video_moderated(result.video_id):
            ...     # post-render moderation caught it — retry with different frames
            ...     ...
        """
        data = await self._api_request("POST", MEDIA_POST_GET_ENDPOINT, {"id": video_id})
        post = data.get("post", data)

        def _is_moderated_obj(obj: dict) -> bool:
            if not obj.get("mediaUrl"):
                return True
            thumb = obj.get("thumbnailImageUrl") or ""
            return MODERATED_THUMBNAIL_UUID in thumb

        # Check the root post; if it has child videos, also check the first
        # one (for upload2vid the root is the container and the video sits
        # under videos[0] / childPosts).
        if _is_moderated_obj(post):
            return True
        videos = post.get("videos") or []
        return bool(videos and _is_moderated_obj(videos[0]))

    async def get_asset_file_size(self, asset_url: str) -> int:
        """Get file size of a Grok asset via HEAD request."""
        self._validate_asset_url(asset_url)
        return await self._asset_request_head(asset_url)

    async def validate_auth(self) -> bool:
        """Check if current authentication is working."""
        try:
            await self._api_request("POST", MEDIA_POST_LIST_ENDPOINT, {"limit": 1, "filter": {}})
            return True
        except (GrokAuthError, GrokAPIError):
            return False

    async def favorite_post(self, post_id: str) -> bool:
        """Add a post to favorites."""
        await self._api_request("POST", MEDIA_POST_LIKE_ENDPOINT, {"id": post_id})
        return True

    async def unfavorite_post(self, post_id: str) -> bool:
        """Remove a post from favorites."""
        await self._api_request("POST", MEDIA_POST_UNLIKE_ENDPOINT, {"id": post_id})
        return True

    async def match_local_video(self, local_path: str | Path) -> VideoMatchResult:
        """Match a local grok video to its web counterpart."""
        local_path = Path(local_path)

        if not local_path.exists():
            raise GrokAPIError(f"File not found: {local_path}")

        filename = local_path.name
        local_size = local_path.stat().st_size

        fmt, extracted_uuid = self._parse_video_filename(filename)

        if fmt == "old":
            # Try 1: Treat as parent_id
            try:
                return await self._match_by_parent_id(extracted_uuid, local_size, filename)
            except (GrokNotFoundError, GrokAPIError):
                pass

            # Try 2: Treat as video_id
            try:
                return await self._match_by_video_id(extracted_uuid, local_size, filename)
            except (GrokNotFoundError, GrokAPIError):
                pass

            # Try 3: Fallback - search by file size
            return await self._match_by_file_size_via_favorites(
                local_size, filename, hint_uuid=extracted_uuid
            )

        elif fmt == "web":
            return await self._match_by_video_id(extracted_uuid, local_size, filename)

        else:
            raise GrokAPIError(
                f"Invalid filename format. Expected 'grok-video-{{uuid}}.mp4' or "
                f"'{{uuid}}.mp4' or '{{uuid}}_hd.mp4', got: {filename}"
            )

    async def _match_by_parent_id(
        self, parent_id: str, local_size: int, filename: str
    ) -> VideoMatchResult:
        """Match video by parent ID."""
        details = await self.get_post_details(parent_id)

        videos_to_check = []

        if details.mode == MODE_TXT2VID and details.hd_media_url:
            videos_to_check.append(
                {
                    "video_id": details.id,
                    "url": details.hd_media_url,
                    "is_parent": True,
                    "prompt": details.original_prompt,
                }
            )

        for child in details.children:
            url = child.hd_media_url or child.media_url
            if url:
                videos_to_check.append(
                    {
                        "video_id": child.id,
                        "url": url,
                        "is_parent": False,
                        "prompt": child.original_prompt,
                    }
                )

        for video in videos_to_check:
            try:
                web_size = await self.get_asset_file_size(video["url"])
                if web_size == local_size:
                    new_filename = f"grok-video_{parent_id}_{video['video_id']}.mp4"
                    return VideoMatchResult(
                        parent_id=parent_id,
                        video_id=video["video_id"],
                        is_parent_video=video["is_parent"],
                        mode=details.mode,
                        original_prompt=video["prompt"],
                        file_size=local_size,
                        new_filename=new_filename,
                    )
            except Exception:
                continue

        raise GrokAPIError(
            f"No matching video found on web for local file: {filename}\n"
            f"Local size: {local_size} bytes\n"
            f"Parent ID: {parent_id}\n"
            f"Videos checked: {len(videos_to_check)}"
        )

    async def _match_by_video_id(
        self, video_id: str, local_size: int, filename: str
    ) -> VideoMatchResult:
        """Match video by video ID - O(1) direct lookup."""
        try:
            details = await self.get_post_details(video_id)
        except GrokNotFoundError:
            return await self._match_by_video_id_via_favorites(video_id, local_size, filename)
        except GrokAuthError:
            raise
        except Exception as e:
            raise GrokAPIError(
                f"Failed to get video details: {video_id}\nLocal file: {filename}\nError: {e}"
            ) from e

        url = self._extract_media_url(details, video_id, filename)
        web_size = await self.get_asset_file_size(url)

        parent_id, is_parent_video = self._extract_parent_info(details, video_id)
        self._verify_file_size_match(video_id, filename, local_size, web_size)
        return self._build_video_match_result(
            parent_id, video_id, is_parent_video, details, local_size
        )

    async def _match_by_video_id_via_favorites(
        self, video_id: str, local_size: int, filename: str, max_posts: int | None = None
    ) -> VideoMatchResult:
        """Search all favorites to find parent of orphaned child video."""
        posts = await self.list_posts(limit=max_posts, source="MEDIA_POST_SOURCE_LIKED")

        for post_summary in posts:
            try:
                details = await self.get_post_details(post_summary.id)

                for child in details.children:
                    if child.id == video_id:
                        parent_id = post_summary.id
                        url = child.hd_media_url or child.media_url

                        if not url:
                            continue

                        try:
                            web_size = await self.get_asset_file_size(url)
                        except Exception:
                            web_size = local_size

                        self._verify_file_size_match(video_id, filename, local_size, web_size)

                        return VideoMatchResult(
                            parent_id=parent_id,
                            video_id=video_id,
                            is_parent_video=False,
                            mode=details.mode,
                            original_prompt=child.original_prompt,
                            file_size=local_size,
                            new_filename=f"grok-video_{parent_id}_{video_id}.mp4",
                        )
            except Exception:
                continue

        raise GrokAPIError(
            f"Video not found in all favorites.\n"
            f"Video ID: {video_id}\n"
            f"Local file: {filename}\n"
        )

    async def _match_by_file_size_via_favorites(
        self,
        local_size: int,
        filename: str,
        hint_uuid: str | None = None,
        max_posts: int | None = None,
    ) -> VideoMatchResult:
        """Search all liked posts to find video by file size."""
        posts = await self.list_posts(limit=max_posts, source="MEDIA_POST_SOURCE_LIKED")

        for post_summary in posts:
            try:
                details = await self.get_post_details(post_summary.id)

                # Check parent video (for txt2vid posts)
                if details.mode == MODE_TXT2VID and details.hd_media_url:
                    try:
                        web_size = await self.get_asset_file_size(details.hd_media_url)
                        if web_size == local_size:
                            return VideoMatchResult(
                                parent_id=details.id,
                                video_id=details.id,
                                is_parent_video=True,
                                mode=details.mode,
                                original_prompt=details.original_prompt,
                                file_size=local_size,
                                new_filename=f"grok-video_{details.id}_{details.id}.mp4",
                            )
                    except Exception:
                        pass

                # Check all children
                for child in details.children:
                    url = child.hd_media_url or child.media_url
                    if not url:
                        continue

                    try:
                        web_size = await self.get_asset_file_size(url)
                        if web_size == local_size:
                            return VideoMatchResult(
                                parent_id=post_summary.id,
                                video_id=child.id,
                                is_parent_video=False,
                                mode=details.mode,
                                original_prompt=child.original_prompt,
                                file_size=local_size,
                                new_filename=f"grok-video_{post_summary.id}_{child.id}.mp4",
                            )
                    except Exception:
                        continue

            except Exception:
                continue

        hint_msg = f" (extracted UUID: {hint_uuid})" if hint_uuid else ""
        raise GrokAPIError(
            f"No matching video found by file size in all favorites.\n"
            f"Local file: {filename}{hint_msg}\n"
            f"Local size: {local_size} bytes\n"
        )

    # =========================================================================
    # GrokClient-specific methods
    # =========================================================================

    @staticmethod
    def generate_stable_id() -> str:
        """Generate a valid Statsig stable_id.

        Format: base64(70 random bytes) with padding stripped.
        This matches the format used by Statsig SDK.

        Returns:
            A 94-character base64-encoded string.

        Example:
            >>> stable_id = GrokClient.generate_stable_id()
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
            >>> new_id = GrokClient.generate_stable_id()
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
                    menu_btn = await self._tab.query_selector(selector)
                    if menu_btn:
                        break
                except Exception:
                    pass
            if menu_btn:
                break
            await asyncio.sleep(2 * d)

        if menu_btn is None:
            raise GrokAPIError("Could not find '...' menu button (Options/更多选项)")

        # Pause any playing video first (video overlay can intercept clicks)
        await self._tab.evaluate('document.querySelectorAll("video").forEach(v => v.pause())')
        await asyncio.sleep(0.3)

        await menu_btn.scroll_into_view()
        await asyncio.sleep(0.5 * d)
        # Radix dropdown requires pointer events (not just click or mouse_click)
        await self._tab.evaluate("""
            (function() {
                var selectors = [
                    'button[aria-label="更多选项"]',
                    'button[aria-label="More options"]',
                    'button[aria-label="Options"]'
                ];
                for (var sel of selectors) {
                    var btn = document.querySelector(sel);
                    if (btn) {
                        btn.dispatchEvent(new PointerEvent("pointerdown", {bubbles: true}));
                        btn.dispatchEvent(new PointerEvent("pointerup", {bubbles: true}));
                        btn.dispatchEvent(new MouseEvent("click", {bubbles: true}));
                        return;
                    }
                }
            })()
        """)
        await asyncio.sleep(1 * d)

        return True

    async def _click_menu_item(self, *text_options: str) -> bool:
        """
        Click a menu item by its text (supports multiple language options).

        Uses mouse_click() which works better than JS click()
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
            items = await self._tab.query_selector_all('[role="menuitem"]')

            for item in items:
                # Get text property (elements have a .text property)
                item_text = item.text.strip() if item.text else ""

                if item_text in text_options:
                    # Radix menu items need pointer events
                    idx = items.index(item)
                    await self._tab.evaluate(f"""
                        (function() {{
                            var items = document.querySelectorAll('[role="menuitem"]');
                            var item = items[{idx}];
                            if (item) {{
                                item.dispatchEvent(new PointerEvent("pointerdown", {{bubbles: true}}));
                                item.dispatchEvent(new PointerEvent("pointerup", {{bubbles: true}}));
                                item.dispatchEvent(new MouseEvent("click", {{bubbles: true}}));
                            }}
                        }})()
                    """)
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

    async def _get_menu_items_text(self) -> list[str]:
        """Get text of all currently visible menu items."""
        result = await self._tab.evaluate(
            "JSON.stringify(Array.from(document.querySelectorAll('[role=\"menuitem\"]')).map(i => i.textContent.trim()))"
        )
        import json

        return json.loads(result) if result else []

    async def delete_video(self, video_id: str) -> bool:
        """
        Delete a child video (not the parent post).

        If the menu only shows "删除帖子" (delete entire post) instead of
        "删除视频" (delete this video), raises GrokAPIError to prevent
        accidentally deleting the parent post and all its children.

        Use delete_post() if you intentionally want to delete the entire post.

        Args:
            video_id: The child video UUID to delete

        Returns:
            True if deletion was successful (or video already doesn't exist)

        Raises:
            GrokAPIError: If only "delete post" is available (use delete_post instead)
        """
        import asyncio

        d = self._ui_delay

        try:
            await self._open_post_menu(video_id)
        except GrokAPIError as e:
            if "404" in str(e):
                return True  # Already deleted
            raise

        # Check what delete options are available
        menu_items = await self._get_menu_items_text()

        has_delete_video = any(t in menu_items for t in ("删除视频", "Delete video"))
        has_delete_post = any(t in menu_items for t in ("删除帖子", "Delete post"))

        if has_delete_video:
            await self._click_menu_item("删除视频", "Delete video")
            await asyncio.sleep(1 * d)
            await self._click_confirm_button("删除视频", "Delete video", "删除", "Delete")
            await asyncio.sleep(1 * d)
            return True

        if has_delete_post and not has_delete_video:
            raise GrokAPIError(
                f"Video {video_id} can only be deleted by deleting the entire post "
                f"(menu shows '删除帖子' not '删除视频'). "
                f"Use delete_post() instead if this is intentional."
            )

        raise GrokAPIError(f"No delete option found in menu. Available: {menu_items}")

    async def delete_post(self, post_id: str) -> bool:
        """
        Delete an entire post (parent + all children).

        This is destructive — it removes the parent image/video and ALL child
        videos under it. Use delete_video() to remove a single child instead.

        Args:
            post_id: The post UUID to delete

        Returns:
            True if deletion was successful (or post already doesn't exist)
        """
        import asyncio

        d = self._ui_delay

        try:
            await self._open_post_menu(post_id)
        except GrokAPIError as e:
            if "404" in str(e):
                return True
            raise

        await self._click_menu_item("删除帖子", "删除视频", "Delete post", "Delete video")
        await asyncio.sleep(1 * d)
        await self._click_confirm_button(
            "删除帖子", "删除视频", "Delete post", "Delete video", "删除", "Delete"
        )
        await asyncio.sleep(1 * d)

        return True

    async def delete_image(self, post_id: str, thumbnail_index: int) -> bool:
        """
        Delete an image variant by its thumbnail index.

        Navigates to the post, switches to image view, selects the
        thumbnail, opens "..." menu, and clicks "删除图像".

        Args:
            post_id: The post UUID
            thumbnail_index: 1-based thumbnail index to delete

        Returns:
            True if deletion was successful

        Raises:
            GrokAPIError: If thumbnail or delete option not found
        """
        import asyncio

        from .actions.navigation import navigate_to_post
        from .actions.post_image import select_thumbnail
        from .actions.post_media import switch_to_image_view
        from .actions.post_menu import click_menu_item, open_post_menu

        d = self._ui_delay

        await navigate_to_post(self._tab, post_id, delay=d)
        await switch_to_image_view(self._tab, delay=d)
        await select_thumbnail(self._tab, thumbnail_index, delay=d)
        await open_post_menu(self._tab, delay=d)
        await click_menu_item(
            self._tab,
            "删除图像",
            "Delete image",
            delay=d,
        )
        await asyncio.sleep(1 * d)

        # Confirm deletion
        await self._click_confirm_button("删除图像", "Delete image", "删除", "Delete")
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

    async def extend_video(
        self,
        video_id: str,
        timeout: int = 300,
    ) -> VideoExtendResult:
        """
        Extend a video by generating continuation frames via UI menu.

        Navigates to the video post, opens the "..." menu, clicks
        "Extend video" / "延长视频", and monitors CDP for the response.

        Uses the new atomic actions layer (actions.navigation, actions.post_menu,
        actions.network_monitor) instead of inline CSS selectors.

        Args:
            video_id: The video UUID to extend
            timeout: Max seconds to wait for generation (default: 300)

        Returns:
            VideoExtendResult with new video_id and metadata

        Raises:
            GrokAPIError: If video not found, menu item missing, or generation fails
        """
        import asyncio
        import random

        from .actions.navigation import navigate_to_post
        from .actions.network_monitor import CDPMonitor
        from .actions.post_menu import click_menu_item, open_post_menu

        # Navigate to the video post page
        await navigate_to_post(self._tab, video_id, delay=self._ui_delay)

        # Start CDP monitoring before triggering the extend action
        async with CDPMonitor(self._tab, "/app-chat/conversations/new") as monitor:
            await asyncio.sleep(1 + random.uniform(0, 0.5))

            # Open "..." menu and click "Extend video"
            await open_post_menu(self._tab, delay=self._ui_delay)
            await click_menu_item(
                self._tab,
                "扩展视频",
                "延长视频",
                "Extend video",
                "Extend Video",
                delay=self._ui_delay,
            )

            # Wait for the request to fire
            if not await monitor.wait_for_request(timeout=10):
                raise GrokAPIError(
                    "Extend video did not trigger a generation request. "
                    "Menu item may have different text — use get_menu_items() to debug."
                )

            # Wait for response body
            await monitor.wait_for_body(timeout=timeout)

        # Parse response (same NDJSON format as create_video)
        gen_result = parse_video_ndjson_response(
            monitor.body, video_id, statsig_id=monitor.statsig_id
        )

        return VideoExtendResult(
            video_id=gen_result.video_id,
            source_video_id=video_id,
            parent_post_id=gen_result.parent_post_id,
            moderated=gen_result.moderated,
            progress=gen_result.progress,
            mode=gen_result.mode,
            model_name=gen_result.model_name,
            conversation_id=gen_result.conversation_id,
            statsig_id=gen_result.statsig_id,
        )

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

        # Get all menu items (use JSON.stringify for clean return)
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

    async def get_thumbnails(self, post_id: str) -> list[dict]:
        """Get image thumbnails on a post page.

        Navigates to the post, switches to image view, and returns
        all thumbnail buttons.

        Args:
            post_id: The post UUID

        Returns:
            List of dicts: [{"index": 1, "name": "Thumbnail 1", "ref": "..."}]
            Empty list if post has only one image.
        """
        from .actions.navigation import navigate_to_post
        from .actions.post_image import get_thumbnails
        from .actions.post_media import switch_to_image_view

        await navigate_to_post(self._tab, post_id, delay=self._ui_delay)
        await switch_to_image_view(self._tab, delay=self._ui_delay)
        return await get_thumbnails(self._tab)

    async def select_thumbnail(self, post_id: str, index: int) -> bool:
        """Select an image thumbnail on a post page.

        Navigates to the post, switches to image view, and clicks
        the thumbnail at the given 1-based index.

        Args:
            post_id: The post UUID
            index: 1-based thumbnail index

        Returns:
            True if thumbnail was clicked

        Raises:
            GrokAPIError: If thumbnail not found
        """
        from .actions.navigation import navigate_to_post
        from .actions.post_image import select_thumbnail
        from .actions.post_media import switch_to_image_view

        await navigate_to_post(self._tab, post_id, delay=self._ui_delay)
        await switch_to_image_view(self._tab, delay=self._ui_delay)
        return await select_thumbnail(self._tab, index, delay=self._ui_delay)

    async def _download_video_by_url(self, video_url: str, output_path: Path) -> Path:
        """Download a video file by URL using the browser's fetch API."""
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

    async def edit_image(self, params: dict) -> ImageEditResult:
        """Edit an image to generate new variations.

        Navigates to the post, clicks edit, enters the prompt,
        and captures the API response with generated images.

        Args:
            params: Dict with keys from EDIT_KEYS (see grok_web.schema):
                post_id (str): Target post UUID.
                edit_prompt (str): Edit instruction (e.g., 'add sunglasses').
                timeout (int, default 300): Max seconds to wait.

        Returns:
            ImageEditResult with image URLs and moderation info.

        Examples:
            await client.edit_image({
                "post_id": "abc-123",
                "edit_prompt": "add wings",
            })
        """
        from .schema import EDIT_KEYS, validate_params

        p = validate_params(params, EDIT_KEYS)

        post_id = p["post_id"]
        edit_prompt = p["edit_prompt"]
        timeout = p.get("timeout", 60)
        import asyncio
        import json as json_mod

        from ai_dev_browser import cdp

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

        # New UI: "编辑图像" is inside the settings gear menu.
        # Must switch to image view first if video is showing, then open gear → click menuitem.
        from ai_dev_browser.core.ax import click_by_ref
        from ai_dev_browser.core.snapshot import page_find

        # Switch to image view (if video is active, "图片" button is visible)
        ax_result = await page_find(self._tab, text="图片", interactable_only=True)
        for el in ax_result.get("elements", []):
            if el.get("role") == "button" and el.get("name") == "图片":
                await click_by_ref(self._tab, el["ref"])
                await asyncio.sleep(2 * d)
                break

        # Open settings gear dropdown
        clicked = False
        ax_result = await page_find(self._tab, text="设置", interactable_only=True)
        for el in ax_result.get("elements", []):
            if el.get("role") == "button" and el.get("name") == "设置":
                await click_by_ref(self._tab, el["ref"])
                await asyncio.sleep(1 * d)
                clicked = True
                break
        if not clicked:
            # CSS fallback
            btn = await self._tab.query_selector('button[aria-label="设置"]')
            if not btn:
                btn = await self._tab.query_selector('button[aria-label="Settings"]')
            if btn:
                await btn.click()
                await asyncio.sleep(1 * d)
                clicked = True
        if not clicked:
            raise GrokAPIError("Could not find settings gear button")

        # Click "编辑图像" menuitem
        edit_clicked = False
        ax_result = await page_find(self._tab, text="编辑图像", interactable_only=True)
        for el in ax_result.get("elements", []):
            if el.get("role") == "menuitem":
                await click_by_ref(self._tab, el["ref"])
                await asyncio.sleep(2 * d)
                edit_clicked = True
                break
        if not edit_clicked:
            # CSS fallback
            items = await self._tab.query_selector_all('[role="menuitem"]')
            for item in items:
                item_text = item.text.strip() if item.text else ""
                if "编辑图像" in item_text or "Edit image" in item_text:
                    await item.click()
                    await asyncio.sleep(2 * d)
                    edit_clicked = True
                    break
        if not edit_clicked:
            raise GrokAPIError("Could not find '编辑图像' menuitem in settings menu")

        # Fill the edit prompt into ProseMirror/tiptap contenteditable editor
        escaped_prompt = edit_prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        fill_result = await self._tab.evaluate(f"""
            (() => {{
                // Try ProseMirror/tiptap contenteditable first (new UI)
                const editor = document.querySelector('.tiptap.ProseMirror') ||
                               document.querySelector('[contenteditable="true"]') ||
                               document.querySelector('.ProseMirror');
                if (editor) {{
                    editor.focus();
                    editor.innerHTML = '<p>{escaped_prompt}</p>';
                    editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    return 'editor';
                }}
                // Fallback: textarea
                const textarea = document.querySelector('textarea');
                if (textarea && textarea.offsetParent !== null) {{
                    textarea.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(textarea, "{escaped_prompt}");
                    textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return 'textarea';
                }}
                return 'not_found';
            }})()
        """)
        if fill_result == "not_found":
            raise GrokAPIError("Could not find edit input (ProseMirror or textarea)")

        await asyncio.sleep(1 * d)

        # Click the submit button: ax_tree name="编辑" or fallback to CSS
        submit_clicked = False
        ax_result = await page_find(self._tab, text="编辑", interactable_only=True)
        for el in ax_result.get("elements", []):
            if el.get("role") == "button" and el.get("name") == "编辑":
                await click_by_ref(self._tab, el["ref"])
                submit_clicked = True
                break
        if not submit_clicked:
            # CSS fallback
            submit_clicked = await self._tab.evaluate("""
                (() => {
                    // Try aria-label
                    let btn = document.querySelector('button[aria-label="编辑"]');
                    if (!btn) btn = document.querySelector('button[aria-label="Edit"]');
                    if (!btn) btn = document.querySelector('button[aria-label="生成视频"]');
                    if (btn) { btn.click(); return true; }
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

    async def _upload_image(self, image_path: str | Path, timeout: int = 15) -> int:
        """Upload a local image to Grok Imagine (internal).

        The image appears as a tag above the input bar. Multiple calls
        upload multiple images (supports "Image 1", "Image 2", etc.).

        Args:
            image_path: Path to the local image file (PNG, JPG, etc.)
            timeout: Max seconds to wait for upload confirmation (default 15)

        Returns:
            Number of images currently attached (e.g., 1 after first upload).
        """
        from .actions.imagine_input import navigate_to_imagine
        from .actions.imagine_input import upload_image as _upload

        await navigate_to_imagine(self._tab, delay=self._ui_delay)
        return await _upload(self._tab, image_path, timeout=timeout, delay=self._ui_delay)

    async def upload_images(self, params: dict) -> list[str]:
        """Upload local image files and return reusable reference strings.

        Each returned string is of the form ``"file:<fileMetadataId>"`` and
        can be passed back to :meth:`create_video` as an ``images`` entry.
        This avoids re-uploading when retrying generation (e.g. after the
        server moderates the first attempt's output video even though the
        images themselves passed moderation).

        Args:
            params: Dict with one required key:

                - images (list[str]): Local image file paths to upload.

        Returns:
            List of ``"file:<uuid>"`` strings, one per input path, in order.

        Example:
            >>> refs = await client.upload_images({"images": ["a.jpg", "b.jpg"]})
            >>> refs
            ['file:477c03f8-...', 'file:09b7e799-...']
            >>> # Retry up to 3 times without re-uploading. Use
            >>> # verify_final=True to also catch post-render moderation
            >>> # (see client.check_video_moderated for details).
            >>> for _ in range(3):
            ...     res = await client.create_video({
            ...         "images": refs, "prompt": "@1 @2", "verify_final": True,
            ...     })
            ...     if not res.moderated:
            ...         break
        """
        from .actions.direct_rest import capture_upload_file_id
        from .actions.imagine_input import (
            navigate_to_imagine,
            remove_all_images,
        )
        from .actions.imagine_input import upload_image as _upload
        from .schema import UPLOAD_KEYS, validate_params

        p = validate_params(params, UPLOAD_KEYS)
        image_paths = p.get("images", [])
        if not image_paths:
            raise ValueError("upload_images requires 'images' list with at least one path")

        await navigate_to_imagine(self._tab, delay=self._ui_delay)
        await remove_all_images(self._tab, delay=self._ui_delay)

        refs: list[str] = []
        for path in image_paths:
            data = await capture_upload_file_id(
                self._tab,
                lambda p=path: _upload(self._tab, p, timeout=15, delay=self._ui_delay),
            )
            file_id = data.get("fileMetadataId")
            if not file_id:
                raise GrokAPIError(f"Upload of {path} did not return a fileMetadataId")
            refs.append(f"file:{file_id}")

        return refs

    async def _create_video_from_file_ids(
        self,
        file_ids: list[str],
        prompt: str = "",
        duration: int = 10,
        resolution: str = "720p",
        aspect_ratio: str | None = None,
        preset: str = "normal",
        timeout: int = 300,
    ) -> VideoGenerationResult:
        """Submit video generation directly via REST, reusing uploaded file IDs.

        Bypasses the UI flow entirely:
        - No upload (caller already uploaded via :meth:`upload_images`)
        - No mode/options/prompt UI interaction
        - No click_submit

        Relies on a recently-captured x-statsig-id (populated passively by
        StatsigSnitch from ordinary page telemetry). Without a fresh token
        the server rejects the POST as anti-bot.
        """
        from .actions.direct_rest import (
            build_video_submit_payload,
            create_media_post,
            direct_submit_video,
        )

        # We need the fileUri alongside the fileMetadataId for the `message`
        # field. Reconstruct from the known per-user scheme.
        if not self.cookies or not self.cookies.x_userid:
            raise GrokAPIError("Cannot reconstruct asset URIs without x-userid cookie")
        user_id = self.cookies.x_userid
        file_uris = [f"users/{user_id}/{fid}/content" for fid in file_ids]

        # Snitch caches sids per-endpoint. A prior UI-triggered create_video
        # populates both /rest/media/post/create and
        # /rest/app-chat/conversations/new.
        snitch = self._statsig_snitch
        if snitch is None:
            raise GrokAPIError("Direct REST submit requires a StatsigSnitch on the client")
        create_sid = await snitch.get("/rest/media/post/create", timeout=2.0)
        conv_sid = await snitch.get("/rest/app-chat/conversations/new", timeout=2.0)
        if not (create_sid and conv_sid):
            raise GrokAPIError(
                "Direct REST submit requires cached x-statsig-id tokens from a "
                "prior UI-triggered create_video(). Run at least one "
                "create_video() with local file paths before using 'file:' "
                "references on the same client."
            )

        # Step 1: ask Grok to register a new post (parentPostId). Using a
        # client-made UUID here returns 404 "Source post not found".
        parent_post_id = await create_media_post(
            self._tab,
            statsig_id=create_sid,
            prompt=prompt,
            media_type="MEDIA_POST_TYPE_VIDEO",
        )

        # Step 2: submit the video generation request.
        payload = build_video_submit_payload(
            file_ids=file_ids,
            file_uris=file_uris,
            parent_post_id=parent_post_id,
            prompt=prompt,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
        )
        # `preset` is unused in the direct path — the "--mode=normal/custom"
        # tag is driven by prompt presence inside build_video_submit_payload,
        # matching what the UI actually sends.
        _ = preset

        try:
            response_text = await direct_submit_video(
                self._tab,
                payload=payload,
                statsig_id=conv_sid,
                timeout=float(timeout),
            )
        except RuntimeError as e:
            # x-statsig-id appears to be effectively single-use per endpoint;
            # after one successful direct submit, the cached sid is stale and
            # Grok returns HTTP 403 anti-bot. Give the caller a clear signal
            # rather than letting the cryptic error bubble up.
            if "403" in str(e):
                # Invalidate the cache so a subsequent UI-path create_video
                # can repopulate from fresh telemetry.
                snitch._by_endpoint.pop("/rest/app-chat/conversations/new", None)
                snitch._by_endpoint.pop("/rest/media/post/create", None)
                raise GrokAPIError(
                    "Direct REST submit rejected by anti-bot (cached "
                    "x-statsig-id consumed). Re-prime by calling "
                    "create_video() once with local file paths, then retry."
                ) from e
            raise

        return parse_video_ndjson_response(
            response_text, parent_post_id=parent_post_id, statsig_id=conv_sid
        )

    async def _create_video_from_upload(
        self,
        image_paths: list[str | Path],
        prompt: str = "",
        timeout: int = 300,
        duration: int = 10,
        resolution: str = "720p",
        aspect_ratio: str | None = None,
    ) -> VideoGenerationResult:
        """Generate video from local image(s) using the Imagine homepage flow.

        Supports multiple images with @N references in the prompt.

        Flow:
        1. Navigate to grok.com/imagine
        2. Upload all images via file input
        3. Switch to video mode
        4. Set video options (resolution, duration, aspect ratio)
        5. Set prompt (with @N references if present)
        6. Click submit and capture NDJSON response

        Args:
            image_paths: List of local image file paths
            prompt: Optional prompt text. Use @1, @2... to reference uploaded images.
            timeout: Max seconds to wait for video generation (default 300)
            duration: Video duration in seconds (6 or 10, default 10)
            resolution: Video resolution ("480p" or "720p", default "720p")
            aspect_ratio: Video aspect ratio (e.g., "2:3", "16:9")

        Returns:
            VideoGenerationResult with video_id and metadata.
        """
        import asyncio
        import random

        from ai_dev_browser import cdp

        from .actions.imagine_input import (
            check_moderated_images,
            click_submit,
            navigate_to_imagine,
            remove_all_images,
            set_mode,
            set_prompt,
            set_prompt_with_refs,
            set_video_options,
        )
        from .actions.imagine_input import (
            upload_image as _upload,
        )
        from .actions.network_monitor import CDPMonitor
        from .prompt_parser import parse_prompt

        # Step 0: Fast health-check the tab. If the browser died between
        # calls (e.g. after a previous upload-moderation raise the user
        # observed Chrome on port N disappear), we want to surface that
        # here as a clear error rather than hanging later on a CDP call
        # whose target is gone.
        try:
            await asyncio.wait_for(
                self._tab.evaluate("1", await_promise=False, return_by_value=True),
                timeout=5.0,
            )
        except Exception as e:
            raise GrokAPIError(
                f"Browser tab appears unresponsive or closed "
                f"(health check failed: {type(e).__name__}: {e}). "
                f"This usually means Chrome crashed or the debug port is "
                f"no longer reachable. Exit and re-enter the get_client() "
                f"context to recover."
            ) from e

        # Step 1: Navigate to imagine homepage (clean state)
        await navigate_to_imagine(self._tab, delay=self._ui_delay)
        await remove_all_images(self._tab, delay=self._ui_delay)

        # Step 2: Upload all images, sniffing fileMetadataIds from the
        # /rest/app-chat/upload-file responses so the caller can later retry
        # without re-uploading (see VideoGenerationResult.image_file_ids).
        # ai-dev-browser has no remove_handler, so we install exactly one
        # handler and gate its body with an 'active' flag — when this
        # call exits (success or raise), the handler becomes a no-op.
        captured_file_ids: list[str] = []
        seen_upload_req_id: dict[str, int | None] = {"id": None}
        sniff_state: dict[str, bool] = {"active": True}

        async def _sniff_upload(event):
            if not sniff_state["active"]:
                return
            if "/rest/app-chat/upload-file" in event.response.url:
                seen_upload_req_id["id"] = event.request_id

        await self._tab.send(cdp.network.enable())
        self._tab.add_handler(cdp.network.ResponseReceived, _sniff_upload)

        try:
            for path in image_paths:
                seen_upload_req_id["id"] = None
                await _upload(self._tab, path, delay=self._ui_delay)
                req_id = seen_upload_req_id["id"]
                if req_id:
                    try:
                        body = await self._tab.send(
                            cdp.network.get_response_body(request_id=req_id)
                        )
                        body_text = body[0] if isinstance(body, tuple) else body
                        import json as _json

                        fid = _json.loads(body_text).get("fileMetadataId")
                        if fid:
                            captured_file_ids.append(fid)
                    except Exception:
                        pass  # best-effort; upload already succeeded

            # Step 2.5: Wait briefly then check for moderated images
            await asyncio.sleep(2)
            moderated = await check_moderated_images(self._tab)
            if moderated:
                total = len(image_paths)
                mod_indices = [i + 1 for i in moderated]  # 1-based for user
                mod_files = [str(image_paths[i]) for i in moderated if i < len(image_paths)]
                raise GrokAPIError(
                    f"{len(moderated)} of {total} images were moderated by Grok "
                    f"(images {mod_indices}): {mod_files}. "
                    "All images must pass moderation to proceed."
                )
        except BaseException:
            # Stop the sniff handler early on ANY exit path, not just the
            # happy one. We can't remove the handler from the tab, so mark
            # it inactive — subsequent fires become cheap no-ops instead
            # of accumulating over repeated create_video() calls.
            sniff_state["active"] = False
            raise

        # Step 3: Switch to video mode
        await set_mode(self._tab, "视频", delay=self._ui_delay)

        # Step 4: Set video options
        await set_video_options(
            self._tab,
            resolution=resolution,
            duration=duration,
            aspect_ratio=aspect_ratio,
            delay=self._ui_delay,
        )

        # Step 5: Set prompt — use @ref parser if images were uploaded
        if prompt:
            segments = parse_prompt(prompt, [str(p) for p in image_paths])
            has_refs = any(s["type"] == "ref" for s in segments)
            if has_refs:
                await set_prompt_with_refs(self._tab, segments, delay=self._ui_delay)
            else:
                await set_prompt(self._tab, prompt, delay=self._ui_delay)

        await asyncio.sleep(random.uniform(0.3, 0.8))

        # Step 6: Set up network monitor, click submit, capture response.
        #
        # Grok's frontend streams the /app-chat/conversations/new response as
        # NDJSON and, as soon as it sees the generated video_id in the stream,
        # router.push's to /imagine/post/{video_id}. That SPA navigation can
        # abort the in-flight XHR BEFORE the browser fires LoadingFinished
        # — the tab ends up on the post page, the video exists, but our
        # CDPMonitor (which only watches LoadingFinished) hangs forever.
        #
        # To keep the flow reliable we race two signals: (a) CDPMonitor gets
        # the body the normal way, OR (b) the tab URL lands on
        # /imagine/post/{uuid}. Whichever comes first wins.
        import re as _re

        _post_re = _re.compile(r"/imagine/post/([0-9a-f-]{36})")

        async def _wait_body_or_nav(timeout_s: float) -> tuple[str, str] | None:
            """Return (mode, payload):
            ('body', ndjson_text) — got the full NDJSON body, normal path
            ('url', video_id)     — tab navigated to post page; fall back
            ('failed', reason)    — CDP reported transport-level failure
            """
            start = asyncio.get_event_loop().time()
            while True:
                if monitor.body is not None:
                    return ("body", monitor.body)
                if monitor.failed_reason is not None:
                    return ("failed", monitor.failed_reason)
                try:
                    cur = await asyncio.wait_for(
                        self._tab.evaluate(
                            "window.location.href",
                            await_promise=False,
                            return_by_value=True,
                        ),
                        timeout=2.0,
                    )
                except Exception:
                    cur = ""
                if isinstance(cur, str):
                    m = _post_re.search(cur)
                    if m:
                        return ("url", m.group(1))
                if asyncio.get_event_loop().time() - start > timeout_s:
                    return None
                await asyncio.sleep(1.0)

        async def _snapshot_tab_diagnostics() -> str:
            """Gather a short human-readable blob describing the tab state
            when a submit hangs — saves a round-trip to the user for the
            next bug report."""
            probes: list[str] = []
            try:
                url = await asyncio.wait_for(
                    self._tab.evaluate(
                        "window.location.href",
                        await_promise=False,
                        return_by_value=True,
                    ),
                    timeout=2.0,
                )
                probes.append(f"url={url!r}")
            except Exception as e:
                probes.append(f"url=<evaluate failed: {e}>")
            try:
                # Visible toast/banner text, if any. Grok tends to surface
                # rate-limit / anti-abuse messages via role=status or
                # role=alert nodes, plus body text scrap.
                err_text = await asyncio.wait_for(
                    self._tab.evaluate(
                        """
                        (function() {
                            var parts = [];
                            document.querySelectorAll(
                              '[role=status], [role=alert], [aria-live]'
                            ).forEach(n => {
                                var t = (n.textContent || '').trim();
                                if (t) parts.push(t.substring(0, 200));
                            });
                            return parts.join(' | ');
                        })()
                        """,
                        await_promise=False,
                        return_by_value=True,
                    ),
                    timeout=2.0,
                )
                probes.append(f"toasts={err_text!r}")
            except Exception as e:
                probes.append(f"toasts=<evaluate failed: {e}>")
            probes.append(f"monitor.request_id={monitor.request_id!r}")
            probes.append(f"monitor.body_received={monitor.body is not None}")
            probes.append(f"monitor.failed_reason={monitor.failed_reason!r}")
            probes.append(f"monitor.statsig_id_captured={monitor.statsig_id is not None}")
            return " ; ".join(probes)

        async with CDPMonitor(self._tab, "/app-chat/conversations/new") as monitor:
            await click_submit(self._tab, delay=self._ui_delay)

            if not await monitor.wait_for_request(timeout=8):
                raise GrokAPIError("Submit did not trigger video generation request")

            outcome = await _wait_body_or_nav(timeout_s=float(timeout))
            if outcome is None:
                # Timed out. Snapshot the tab so the next bug report has
                # enough context to distinguish Grok-side rate limiting
                # from a transport-level drop we didn't observe in time.
                diag = await _snapshot_tab_diagnostics()
                raise GrokAPIError(
                    f"Timed out ({timeout}s) waiting for video generation "
                    f"response. Neither NDJSON body nor post-page "
                    f"navigation observed. Diagnostics: {diag}"
                )

        mode, payload = outcome
        if mode == "body":
            # Happy path — parse the full NDJSON stream.
            result = parse_video_ndjson_response(
                payload, parent_post_id="", statsig_id=monitor.statsig_id
            )
        elif mode == "failed":
            # CDP told us the request's transport dropped (TCP reset,
            # net::ERR_ABORTED, etc.). We have no body and no video_id.
            # Attach any tab-state context the user might want to see.
            diag = await _snapshot_tab_diagnostics()
            raise GrokAPIError(
                f"Video generation request failed at transport level "
                f"(CDP LoadingFailed: {payload}). This usually means "
                f"Grok dropped the connection — possible causes include "
                f"anti-abuse rate limiting after prior moderation events, "
                f"auth expiry, or a network blip. Diagnostics: {diag}"
            )
        else:  # mode == "url"
            # Fallback — XHR was aborted by SPA nav but Grok still completed
            # the generation. Reconstruct a VideoGenerationResult from the
            # post's REST record. Cap the REST read at 15s so a stuck
            # /rest/media/post/get doesn't silently stall the whole call.
            video_id = payload
            logger.warning(
                f"CDP NDJSON body never arrived for video {video_id}; "
                f"recovering via /rest/media/post/get (happens when Grok's "
                f"frontend router.push's before the XHR stream closes)."
            )
            try:
                details = await asyncio.wait_for(
                    self.get_post_details(video_id),
                    timeout=15.0,
                )
                raw = details.raw_data.get("post", details.raw_data) if details.raw_data else {}
                # Minimal fields — parse_video_ndjson_response's output shape
                result = VideoGenerationResult(
                    video_id=video_id,
                    parent_post_id=raw.get("originalPostId") or video_id,
                    moderated=False,  # verify_final (if set) will re-check
                    progress=100,
                    mode=raw.get("mode") or "normal",
                    model_name=raw.get("modelName"),
                    image_reference=None,
                    conversation_id=None,
                    statsig_id=monitor.statsig_id,
                )
            except asyncio.TimeoutError as e:
                raise GrokAPIError(
                    f"NDJSON body missing; REST recovery via get_post_details "
                    f"timed out after 15s. Video {video_id} exists in Grok but "
                    f"/rest/media/post/get is not returning — likely Grok-side "
                    f"rate limiting, or the post has not been indexed yet."
                ) from e
            except Exception as e:
                raise GrokAPIError(
                    f"NDJSON body missing and REST recovery failed: {e}. "
                    f"The video was created (id={video_id}) but its details "
                    f"could not be fetched."
                ) from e

        # Attach uploaded file IDs so the caller can retry via 'file:' refs
        # (bypasses both re-upload and the UI flow on subsequent calls).
        result.image_file_ids = captured_file_ids
        # Stop the upload sniffer now that this call has finished. Each
        # create_video() installs its own closure-scoped sniffer; without
        # this, repeated calls accumulate dead handlers.
        sniff_state["active"] = False
        return result

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
        params: dict,
        *,
        progress_callback: "Callable[[int], Awaitable[bool]] | None" = None,
    ) -> ImageGenerationResult:
        """Generate images from a text prompt (txt2img).

        Navigates to grok.com/imagine, selects Image mode, enters the prompt,
        and captures generated images via WebSocket. Scrolls for more if needed.

        IMPORTANT: Generated images are temporary! The gallery disappears on refresh.

        Args:
            params: Dict with keys from IMAGE_KEYS (see grok_web.schema):
                images (list[str]): Not used for create_image currently.
                prompt (str): Text description of the image to generate.
                aspect_ratio (str, default '2:3'): 'portrait', 'landscape', 'square',
                    or ratio like '2:3', '1:1', '3:2'.
                min_success (int, default 1): Minimum non-moderated images needed.
                max_scroll (int, default 5): Max scroll attempts for more images.
                timeout (int, default 300): Max seconds to wait.
                thumbnail_selector (callable): Callback for image selection. Python API only.
            progress_callback: Internal callback for shared target across workers.

        Returns:
            ImageGenerationResult with image URLs and generation info.

        Examples:
            await client.create_image({"prompt": "a cat wearing sunglasses"})

            await client.create_image({
                "prompt": "a cat",
                "aspect_ratio": "portrait",
                "min_success": 10,
                "max_scroll": 8,
            })
        """
        from .schema import IMAGE_KEYS, validate_params

        p = validate_params(params, IMAGE_KEYS)

        prompt = p.get("prompt", "")
        aspect_ratio = p.get("aspect_ratio", "2:3")
        min_success = p.get("min_success", 1)
        max_scroll = p.get("max_scroll", 5)
        timeout = p.get("timeout", 300)
        thumbnail_selector = p.get("thumbnail_selector")
        import asyncio
        import json as json_mod

        from ai_dev_browser import cdp

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
            await model_btn.click()
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
            await model_btn.click()
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
            await submit_btn.click()
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

    async def _create_video_from_text(
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
            >>> result = await client._create_video_from_text("a cat playing with yarn")
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
                await model_btn.click()
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
            await model_btn.click()
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
            await submit_btn.click()
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

                # Handle list format
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
    # Unified Video Generation API (dict-based, SSOT from schema.py)
    # =========================================================================

    async def create_video(self, params: dict) -> VideoGenerationResult:
        """Generate video from text, existing post, or uploaded images.

        Mode is auto-detected from params:
        - No images → txt2vid
        - images with 'post:<uuid>' → img2vid (navigate to post, generate video)
        - images with file paths → upload2vid (upload + generate from Imagine homepage)

        Args:
            params: Dict with keys from VIDEO_KEYS (see grok_web.schema):
                images (list[str]): Image sources. Local file paths (upload),
                    'post:<uuid>' (existing post), or 'file:<uuid>' (previously
                    uploaded via client.upload_images — skips re-upload). Max 5.
                prompt (str): Text prompt. Use @1, @2... to reference images.
                mode (str, default 'video'): 'image' or 'video'.
                resolution (str, default '720p'): '480p', '720p'.
                duration (str, default '10s'): '6s', '10s'.
                aspect_ratio (str, default '2:3'): '2:3', '3:2', '1:1', '9:16', '16:9', etc.
                preset (str): 'normal', 'fun', 'spicy'.
                timeout (int, default 300): Max seconds to wait.
                wait_for_video (bool, default True): Wait for video to load (txt2vid only).
                verify_final (bool, default False): Double-check post-render
                    moderation via REST after the immediate response. See
                    'Moderation' note below.

        Returns:
            VideoGenerationResult with video_id and metadata.
            - result.moderated reflects only the immediate NDJSON verdict
              unless verify_final=True was passed (see below).
            - result.image_file_ids lists the fileMetadataIds of any
              uploaded images; reuse as ['file:<id>', ...] to retry
              generation without re-uploading.

        Moderation (two stages):
            Grok moderates in two passes. The immediate pass checks the
            prompt and reference images; its verdict populates
            result.moderated. The second pass runs AFTER the video
            actually renders — a video can pass the immediate pass and
            still be replaced with a hidden-content placeholder.

            To catch the second pass, either:
              * pass verify_final=True (adds ~150ms, OR'd into moderated), or
              * call client.check_video_moderated(video_id) when you need it.

        Examples:
            # txt2vid
            await client.create_video({"prompt": "a cat dancing"})

            # img2vid from existing post
            await client.create_video({
                "images": ["post:8ddd91f6-..."],
                "prompt": "slow orbit around @1",
            })

            # upload2vid with multiple images
            await client.create_video({
                "images": ["./frame1.jpg", "./frame2.jpg"],
                "prompt": "@1 is the main character, zoom into @2",
                "resolution": "720p",
                "duration": "10s",
            })

            # Retry loop that survives both moderation stages
            refs = None
            for _ in range(5):
                params = {"images": refs or ["./a.jpg", "./b.jpg"],
                          "prompt": "zoom @1 @2",
                          "verify_final": True}
                r = await client.create_video(params)
                if not r.moderated:
                    break
                # Reuse uploaded files on the next attempt (no re-upload).
                refs = [f"file:{fid}" for fid in r.image_file_ids]
        """
        from .schema import VIDEO_KEYS, validate_params

        p = validate_params(params, VIDEO_KEYS)

        images = p.get("images", [])
        prompt = p.get("prompt", "")
        timeout = p.get("timeout", 300)

        # Normalize duration to int
        duration = p.get("duration", "10s")
        if isinstance(duration, str):
            duration = int(duration.replace("s", ""))

        resolution = p.get("resolution", "720p")
        preset = p.get("preset", "normal")
        wait_for_video = p.get("wait_for_video", True)
        verify_final = p.get("verify_final", False)

        # aspect_ratio: default "2:3" for txt2vid, None for upload2vid
        # (Grok UI hides aspect ratio dropdown for multi-image uploads)
        if "aspect_ratio" in params:
            aspect_ratio = p["aspect_ratio"]
        else:
            aspect_ratio = "2:3" if not images else None

        if not images:
            # txt2vid — text prompt only
            result = await self._create_video_from_text(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                timeout=timeout,
                wait_for_video=wait_for_video,
            )
        else:
            # Classify image sources
            from .prompt_parser import classify_image_source

            sources = [classify_image_source(img) for img in images]
            types = {s[0] for s in sources}

            if len(types) > 1:
                raise ValueError(
                    "Cannot mix source types in images list — use only one of: "
                    "'post:<uuid>', 'file:<uuid>' (previously uploaded), or local paths."
                )

            kind = next(iter(types))

            if kind == "post":
                # img2vid — use first post source
                post_id = sources[0][1]
                result = await self._create_video_via_ui(
                    parent_post_id=post_id,
                    preset=preset,
                    timeout=timeout,
                    adjustment_prompt=prompt if prompt else None,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                )
            elif kind == "upload":
                # Previously uploaded file IDs — direct REST path.
                file_ids = [s[1] for s in sources]
                result = await self._create_video_from_file_ids(
                    file_ids=file_ids,
                    prompt=prompt,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    preset=preset,
                    timeout=timeout,
                )
            else:
                # upload2vid — upload file(s) and generate
                file_paths = [s[1] for s in sources]
                result = await self._create_video_from_upload(
                    image_paths=file_paths,
                    prompt=prompt,
                    timeout=timeout,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                )

        # Optional: confirm post-render moderation verdict via REST. The
        # immediate NDJSON response only reflects prompt/ref moderation; a
        # video can pass that and still be blocked after rendering.
        if verify_final and result.video_id and not result.moderated:
            import asyncio as _asyncio_vf

            logger.info(
                "verify_final: probing /rest/media/post/get for %s (15s cap)",
                result.video_id,
            )
            try:
                # Hard cap at 15s: this is a single REST read, no streaming.
                # If it takes longer than that, Grok is throttling / the
                # video isn't indexed yet / something is wrong — don't let
                # it silently hang the whole create_video() call.
                mod = await _asyncio_vf.wait_for(
                    self.check_video_moderated(result.video_id),
                    timeout=15.0,
                )
                if mod:
                    result.moderated = True
                logger.info(
                    "verify_final: moderated=%s for %s",
                    result.moderated,
                    result.video_id,
                )
            except _asyncio_vf.TimeoutError:
                logger.warning(
                    "verify_final: check_video_moderated(%s) timed out after "
                    "15s — leaving result.moderated=%s. Grok may be rate-"
                    "limiting /rest/media/post/get or the video has not yet "
                    "been indexed.",
                    result.video_id,
                    result.moderated,
                )
            except Exception as e:
                logger.warning(
                    f"verify_final check failed ({e}); leaving result.moderated unchanged"
                )

        return result

    async def _create_video_via_ui(
        self,
        parent_post_id: str,
        preset: str = "normal",
        timeout: int = 300,
        stable_id: str | None = None,
        adjustment_prompt: str | None = None,
        duration: int = 10,
        resolution: str = "720p",
        aspect_ratio: str = "2:3",
        thumbnail_index: int | None = None,
    ) -> VideoGenerationResult:
        """
        Generate video by simulating UI button click (more reliable for anti-bot bypass).

        This navigates to the post page, opens the settings gear menu to configure
        video options, then triggers generation via the "制作视频" menu item.

        Args:
            parent_post_id: The image post ID to generate video from
            preset: Video style preset - 'normal', 'fun', or 'spicy'
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
            aspect_ratio: Video aspect ratio (default "2:3").
                         Options: "2:3", "3:2", "1:1", "9:16", "16:9".

        Returns:
            VideoGenerationResult with video_id (may be empty if moderated).
            When adjustment_prompt is used, result.mode will be 'custom'.
        """
        import asyncio

        from ai_dev_browser import cdp

        # Inject custom stable_id if provided
        if stable_id:
            await self.set_stable_id(stable_id, reload_page=False)

        # Normalize preset to string
        preset_str = str(preset).lower()

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

        # Select specific image thumbnail if requested
        if thumbnail_index is not None:
            from .actions.post_image import select_thumbnail
            from .actions.post_media import switch_to_image_view

            await switch_to_image_view(self._tab, delay=self._ui_delay)
            await select_thumbnail(self._tab, thumbnail_index, delay=self._ui_delay)

        # --- New UI: Settings gear menu ---
        # The settings gear (button[aria-label="设置"]) opens a Radix dropdown containing:
        #   - Duration: button[aria-label="6s"] / button[aria-label="10s"]
        #   - Resolution: button[aria-label="480p"] / button[aria-label="720p"]
        #   - Aspect ratio: button[aria-label="2:3"] / "3:2" / "1:1" / "9:16" / "16:9"
        #   - "编辑图像" menuitem
        #   - "制作视频" menuitem (triggers video generation)
        # After entering video mode, extra items appear: presets + "重做"
        # IMPORTANT: Radix dropdown closes after ANY click — must reopen between selections.

        async def _open_settings():
            """Open the settings gear dropdown. Returns True if opened."""
            btn = await self._tab.query_selector('button[aria-label="设置"]')
            if not btn:
                btn = await self._tab.query_selector('button[aria-label="Settings"]')
            if btn:
                # Dropdown menus require real mouse events
                await btn.mouse_click()
                await asyncio.sleep(0.5)
                return True
            return False

        async def _click_menuitem(text: str) -> bool:
            """Find and click a menuitem by text content. Returns True if clicked."""
            menu_items = await self._tab.query_selector_all('[role="menuitem"]')
            for item in menu_items:
                item_text = item.text.strip() if hasattr(item, "text") else ""
                if not item_text:
                    idx = menu_items.index(item)
                    item_text = await self._tab.evaluate(
                        f"document.querySelectorAll('[role=\"menuitem\"]')[{idx}].textContent.trim()",
                        await_promise=False,
                    )
                if text in item_text:
                    await item.click()
                    await asyncio.sleep(0.3)
                    return True
            return False

        try:
            # Select duration (e.g., "10s") — open menu, click, menu closes
            if await _open_settings():
                duration_label = f"{duration}s"
                dur_btn = await self._tab.query_selector(f'button[aria-label="{duration_label}"]')
                if dur_btn:
                    await dur_btn.click()
                    await asyncio.sleep(0.3)

            # Select resolution (e.g., "720p") — reopen menu, click, menu closes
            if await _open_settings():
                res_label = resolution if resolution.endswith("p") else f"{resolution}p"
                res_btn = await self._tab.query_selector(f'button[aria-label="{res_label}"]')
                if res_btn:
                    await res_btn.click()
                    await asyncio.sleep(0.3)

            # Select aspect ratio if non-default — reopen menu, click, menu closes
            if aspect_ratio and aspect_ratio != "2:3" and await _open_settings():
                ar_btn = await self._tab.query_selector(f'button[aria-label="{aspect_ratio}"]')
                if ar_btn:
                    await ar_btn.click()
                    await asyncio.sleep(0.3)
        except Exception:
            pass  # If settings interaction fails, continue with defaults

        # Scroll to ensure buttons are visible (image overlay button may be below fold)
        await self._tab.evaluate(
            "window.scrollTo(0, document.body.scrollHeight / 3)", await_promise=False
        )
        await asyncio.sleep(0.5 + random.uniform(0, 0.3))

        # --- Generation trigger ---
        # New UI flow:
        # - button[aria-label="制作视频"] (image overlay) → triggers first generation
        # - After entering video mode: button[aria-label="生成视频"] (arrow up = regenerate)
        # - Settings dropdown gains presets (Spicy/Fun/Normal) + "重做" in video mode
        # - "输入你的想象" input appears in video mode for adjustment prompts

        async def _click_make_video_button() -> bool:
            """Click the '制作视频' or '生成视频' button to trigger generation."""
            # Try "制作视频" first (initial image post state)
            btn = await self._tab.query_selector('button[aria-label="制作视频"]')
            if not btn:
                btn = await self._tab.query_selector('button[aria-label="Make video"]')
            # Fallback: "生成视频" (video mode state, arrow up = regenerate)
            if not btn:
                btn = await self._tab.query_selector('button[aria-label="生成视频"]')
            if not btn:
                btn = await self._tab.query_selector('button[aria-label="Generate video"]')
            if btn:
                await btn.click()
                return True
            return False

        async def _wait_for_request(wait_timeout: int = 8) -> bool:
            """Wait for a CDP request to be captured. Returns True if captured."""
            wait_start = asyncio.get_event_loop().time()
            while captured_response["request_id"] is None:
                elapsed = asyncio.get_event_loop().time() - wait_start
                if elapsed > wait_timeout:
                    return False
                await asyncio.sleep(0.5)
            return True

        async def _wait_for_body(body_timeout: int = 0) -> None:
            """Wait for response body with timeout."""
            effective_timeout = body_timeout or timeout
            start = asyncio.get_event_loop().time()
            while captured_response["body"] is None:
                elapsed = asyncio.get_event_loop().time() - start
                if elapsed > effective_timeout:
                    raise GrokAPIError("Timeout waiting for video generation response")
                await asyncio.sleep(0.5)

        max_click_retries = 3
        click_wait_timeout = 8

        if preset_str != "normal" or adjustment_prompt:
            # Both non-normal preset and adjustment_prompt require entering video mode first.
            # Step 1: Click "制作视频" to enter video mode (triggers initial Normal generation)
            for click_attempt in range(1, max_click_retries + 1):
                captured_response["request_id"] = None
                await asyncio.sleep(random.uniform(0.3, 0.8))

                clicked = await _click_make_video_button()
                if not clicked and click_attempt == max_click_retries:
                    raise GrokAPIError("Could not find '制作视频' button after retries")
                elif not clicked:
                    await asyncio.sleep(2 + random.uniform(0, 1.0))
                    continue

                if await _wait_for_request(click_wait_timeout):
                    break

                if click_attempt < max_click_retries:
                    await asyncio.sleep(2 + random.uniform(0, 1.5))

            if captured_response["request_id"] is None:
                raise GrokAPIError("'制作视频' button did not trigger video generation request")

            # Wait for first generation to complete
            await _wait_for_body()

            # Now in video mode — reset capture for the second generation
            await asyncio.sleep(2 + random.uniform(0, 1.0))
            captured_response["body"] = None
            captured_response["request_id"] = None

            if adjustment_prompt:
                # Step 2a: Fill the "输入你的想象" input and click "生成视频" (arrow up)
                escaped_prompt = (
                    adjustment_prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                )

                # Try textarea first, then contenteditable (ProseMirror/tiptap editor)
                await self._tab.evaluate(
                    f"""
                    (function() {{
                        // Try textarea
                        const ta = document.querySelector('textarea');
                        if (ta && ta.offsetParent !== null) {{
                            ta.focus();
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, 'value'
                            ).set;
                            setter.call(ta, "{escaped_prompt}");
                            ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return 'textarea';
                        }}
                        // Try contenteditable (ProseMirror/tiptap)
                        const editor = document.querySelector('.tiptap.ProseMirror') ||
                                       document.querySelector('[contenteditable="true"]') ||
                                       document.querySelector('.ProseMirror');
                        if (editor) {{
                            editor.focus();
                            editor.innerHTML = '<p>{escaped_prompt}</p>';
                            editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            return 'editor';
                        }}
                        return 'not_found';
                    }})()
                """,
                    await_promise=False,
                )

                await asyncio.sleep(0.5)

                # Click "生成视频" button (the arrow up / regenerate button)
                for click_attempt in range(1, max_click_retries + 1):
                    captured_response["request_id"] = None
                    await asyncio.sleep(random.uniform(0.3, 0.8))

                    submit_btn = await self._tab.query_selector('button[aria-label="生成视频"]')
                    if not submit_btn:
                        submit_btn = await self._tab.query_selector(
                            'button[aria-label="Generate video"]'
                        )
                    if not submit_btn:
                        submit_btn = await self._tab.query_selector('button[aria-label="提交"]')

                    if submit_btn:
                        await submit_btn.click()
                    elif click_attempt == max_click_retries:
                        raise GrokAPIError("Could not find '生成视频' button after retries")
                    else:
                        await asyncio.sleep(2 + random.uniform(0, 1.0))
                        continue

                    if await _wait_for_request(click_wait_timeout):
                        break

                    if click_attempt < max_click_retries:
                        await asyncio.sleep(2 + random.uniform(0, 1.5))

                if captured_response["request_id"] is None:
                    raise GrokAPIError("'生成视频' button did not trigger request after retries")

            else:
                # Step 2b: Non-normal preset — open settings and click preset menuitem
                for click_attempt in range(1, max_click_retries + 1):
                    captured_response["request_id"] = None
                    await asyncio.sleep(random.uniform(0.3, 0.8))

                    clicked = False
                    if await _open_settings():
                        clicked = await _click_menuitem(preset_menu_text)

                    if not clicked and click_attempt == max_click_retries:
                        raise GrokAPIError(f"Could not find preset '{preset_menu_text}' menu item")

                    if await _wait_for_request(click_wait_timeout):
                        break

                    if click_attempt < max_click_retries:
                        await asyncio.sleep(2 + random.uniform(0, 1.5))

                if captured_response["request_id"] is None:
                    raise GrokAPIError(
                        f"Preset '{preset_menu_text}' did not trigger video generation"
                    )

        else:
            # Normal preset, no adjustment_prompt:
            # Simply click the "制作视频" button on the image overlay
            for click_attempt in range(1, max_click_retries + 1):
                captured_response["request_id"] = None
                await asyncio.sleep(random.uniform(0.3, 0.8))

                clicked = await _click_make_video_button()
                if not clicked and click_attempt == max_click_retries:
                    raise GrokAPIError("Could not find '制作视频' button after retries")
                elif not clicked:
                    await asyncio.sleep(2 + random.uniform(0, 1.0))
                    continue

                if await _wait_for_request(click_wait_timeout):
                    break

                if click_attempt < max_click_retries:
                    await asyncio.sleep(2 + random.uniform(0, 1.5))

            if captured_response["request_id"] is None:
                raise GrokAPIError(
                    f"'制作视频' button did not trigger request after {max_click_retries} attempts"
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

    # =========================================================================
    # Video download
    # =========================================================================

    async def download_video(
        self,
        video_id: str,
        output_path: str | Path,
        *,
        prefer_hd: bool = True,
        parent_post_id: str | None = None,
    ) -> Path:
        """Download a video to local file.

        Args:
            video_id: The child video UUID to download
            output_path: Destination file path (will be created/overwritten)
            prefer_hd: If True (default), download HD version if available
            parent_post_id: Parent post ID (optional, for faster lookup).
                If provided, skips searching through favorites.

        Returns:
            Path to the downloaded file

        Raises:
            GrokNotFoundError: If video not found
            GrokAPIError: If download fails
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

        return await self._download_video_by_url(video_url, output_path)

    # =========================================================================
    # Video thumbnail selection
    # =========================================================================

    async def get_video_thumbnails(self, post_id: str) -> list[dict]:
        """Get video thumbnails on a post page.

        Args:
            post_id: The post UUID

        Returns:
            List of dicts: [{"index": 1, "name": "Thumbnail 1", "ref": "..."}]
        """
        from .actions.navigation import navigate_to_post
        from .actions.post_media import switch_to_video_view
        from .actions.post_video import get_video_thumbnails

        await navigate_to_post(self._tab, post_id, delay=self._ui_delay)
        await switch_to_video_view(self._tab, delay=self._ui_delay)
        return await get_video_thumbnails(self._tab)

    async def select_video_thumbnail(self, post_id: str, index: int) -> bool:
        """Select a video thumbnail by 1-based index.

        Args:
            post_id: The post UUID
            index: 1-based thumbnail index

        Returns:
            True if clicked

        Raises:
            GrokAPIError: If thumbnail not found
        """
        from .actions.navigation import navigate_to_post
        from .actions.post_media import switch_to_video_view
        from .actions.post_video import select_video_thumbnail

        await navigate_to_post(self._tab, post_id, delay=self._ui_delay)
        await switch_to_video_view(self._tab, delay=self._ui_delay)
        return await select_video_thumbnail(self._tab, index, delay=self._ui_delay)

    # =========================================================================
    # Post hierarchy
    # =========================================================================

    async def find_root_post(self, post_id: str) -> PostDetails:
        """Walk up the post tree to find the root post.

        Every post has an original_post_id pointing to its parent.
        This walks up until it reaches a post with no parent (the root).

        Args:
            post_id: Any post UUID (image or video)

        Returns:
            PostDetails of the root post (which contains all descendants
            in its children list).

        Raises:
            GrokNotFoundError: If any post in the chain is not found
        """
        current = await self.get_post_details(post_id)
        # Walk up: max 10 hops as safety against cycles
        for _ in range(10):
            if current.original_post_id is None:
                return current
            current = await self.get_post_details(current.original_post_id)
        raise GrokAPIError(f"Could not find root post after 10 hops from {post_id}")

    # =========================================================================
    # Image-video relationship
    # =========================================================================

    async def get_image_video_map(self, post_id: str) -> list["ImageVideoMapping"]:
        """Get image variants with their child videos for a post.

        Each entry represents a source image (original or edited variant)
        and all videos generated from it.

        Args:
            post_id: The parent post UUID

        Returns:
            List of ImageVideoMapping (post_id, media_url, videos).
        """
        from .models import ImageVideoMapping

        details = await self.get_post_details(post_id)
        groups = details.videos_by_parent_image()

        result = []
        for source_id, videos in groups.items():
            media_url = None
            if source_id == details.id:
                media_url = details.media_url
            else:
                try:
                    source_details = await self.get_post_details(source_id)
                    media_url = source_details.media_url
                except Exception:
                    pass
            result.append(ImageVideoMapping(post_id=source_id, media_url=media_url, videos=videos))

        return result
