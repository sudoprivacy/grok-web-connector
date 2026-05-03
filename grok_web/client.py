"""
Grok Web Connector - GrokClient

Browser automation client using Chrome DevTools Protocol (via ai-dev-browser/CDP).
Handles all Grok API operations: reads, writes, video/image generation, and UI automation.

Public API:
    Use get_client() from grok_web package — returns GrokClient.
"""

import json
import logging
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ai_dev_browser.core.config import DEFAULT_DEBUG_HOST

from ._internal import (
    MEDIA_POST_CREATE_ENDPOINT,
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
# ai-dev-browser launch_chrome stderr patch
# =============================================================================
# ai-dev-browser's launch_chrome calls subprocess.Popen with stderr=PIPE on
# Windows but never drains the pipe. Chrome's stderr (GPU process logs,
# Crashpad/Breakpad chatter, V8 errors, CSP violations, sandbox warnings —
# subsystems NOT covered by --disable-logging / --log-file=NUL) fills the
# kernel pipe buffer (~4-8KB) within a few minutes of CDP-heavy activity →
# Chrome's stderr write blocks → process hangs / aborts. v0.19.13 cut most
# of stderr via Chrome flags (3x lifespan improvement reported in the wild)
# but didn't fully close the gap — the only definitive fix is to redirect
# stderr to DEVNULL at the OS level so Chrome's writes go to the bit bucket
# regardless of which subsystem emits them.
#
# Approach: monkey-patch subprocess.Popen as seen from ai_dev_browser's
# chrome.py module so that any kwargs with stderr=PIPE get rewritten to
# stderr=DEVNULL. Other Popen calls in the process are unaffected (we patch
# the binding inside chrome.py's namespace, not the global subprocess module).
# Idempotent — installed once per process.

_AI_DEV_BROWSER_STDERR_PATCHED = False


def _patch_ai_dev_browser_chrome_stderr() -> None:
    global _AI_DEV_BROWSER_STDERR_PATCHED
    if _AI_DEV_BROWSER_STDERR_PATCHED:
        return
    try:
        import subprocess as _sp

        from ai_dev_browser.core import chrome as _chrome_mod
    except Exception:
        # ai-dev-browser layout changed — bail; v0.19.13 flag-based cut
        # is still in effect via extra_args, so we degrade gracefully.
        return

    _OriginalPopen = _chrome_mod.subprocess.Popen

    class _StderrDevnullPopen(_OriginalPopen):
        def __init__(self, *args, **kwargs):
            if kwargs.get("stderr") is _sp.PIPE:
                kwargs["stderr"] = _sp.DEVNULL
            super().__init__(*args, **kwargs)

    # Replace subprocess.Popen as seen from chrome.py only. The launch_chrome
    # function does ``subprocess.Popen(args, **popen_kwargs)`` against this
    # name, so reassigning here intercepts that call without globally
    # affecting the user's other subprocess.Popen usage.
    class _SubprocessShim:
        def __getattr__(self, name):
            if name == "Popen":
                return _StderrDevnullPopen
            return getattr(_sp, name)

    _chrome_mod.subprocess = _SubprocessShim()  # type: ignore[assignment]
    _AI_DEV_BROWSER_STDERR_PATCHED = True
    logger.debug(
        "[chrome-stderr-patch] subprocess.Popen wrapped to force stderr=DEVNULL on Chrome launch"
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
        startup_timeout: float = 30.0,
        extra_chrome_args: list[str] | None = None,
        user_data_dir: str | Path | None = None,
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
            startup_timeout: Seconds to wait for Chrome to bind the debug port
                     on auto-launch (default: 30.0). Raise on slow/crowded
                     Windows machines or first-time profile init.
            extra_chrome_args: Additional Chrome command-line flags appended
                     to ai-dev-browser's defaults + the connector's
                     ``--disable-logging`` / ``--log-file=NUL`` defaults
                     (which silence Chrome's stderr to prevent the
                     long-running pipe-buffer-fill hang on Windows). Use
                     this only if you need Chrome flags beyond what
                     ai-dev-browser and connector already supply.
            user_data_dir: Absolute path for Chrome's ``--user-data-dir``.
                     Default: ``~/.grok-web-connector/profiles/<profile>/``
                     — DELIBERATELY OUTSIDE ai-dev-browser's managed namespace
                     (``~/.ai-dev-browser/profiles/...``) so other agents'
                     ``browser_cleanup()`` calls cannot classify our Chrome
                     as an "orphan" and kill it. Persistent across runs by
                     profile name; safe under multi-agent concurrent use.
                     Pass an explicit path only if you want a specific
                     location for backups / inspection.
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
        # One-shot flag so create_image's "you're not persisting" nudge
        # fires at most once per client — avoids log spam on batch runs
        # where the caller has made an informed opt-out choice.
        self._persistence_hinted = False
        # Same for create_video / extend_video's "Grok auto-favorited
        # the source" nudge.
        self._favorite_pollution_hinted = False
        # 2026-05: Grok's frontend now clears the create_image gallery
        # state immediately after the last image lands (previously the
        # gallery stayed visible for hours of user browsing). Hint once
        # per client when create_image finishes without auto_favorite,
        # so callers who relied on manual browse + heart-button workflow
        # know to switch to auto_favorite=N.
        self._gallery_ephemeral_hinted = False

        # Snitch for x-statsig-id (populated passively from any grok.com request
        # seen on this tab). Used by direct REST submit to pass anti-bot check.
        self._statsig_snitch = None

        # Browser connection settings
        self._remote_host = host or DEFAULT_DEBUG_HOST
        self._remote_port = port  # None = let start_browser auto-assign
        self._auto_launch = auto_launch
        self._force_new_chrome = force_new_chrome
        self._profile = profile
        self._startup_timeout = startup_timeout
        self._extra_chrome_args = list(extra_chrome_args) if extra_chrome_args else None
        # Resolved later in __aenter__ when profile_name is finalized; storing
        # the user's literal value (or None for "auto") here.
        self._user_data_dir = Path(user_data_dir).resolve() if user_data_dir else None

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

    def _launch_or_reuse_chrome(
        self,
        *,
        user_data_dir: Path,
        requested_port: int | None,
        headless: bool,
        extra_args: list[str],
        force_new: bool,
        startup_timeout: float,
    ) -> tuple[int, bool]:
        """Launch a Chrome with explicit user_data_dir, or reuse one already
        listening on requested_port.

        Replaces ai-dev-browser's `browser_start` so we can control
        ``--user-data-dir`` (which browser_start hardcodes inside its managed
        namespace). Returns ``(port, reused_bool)``.

        Reuse rule: only reuse when caller explicitly pinned a port AND that
        port is already accepting CDP connections AND we're not in
        force_new mode. We don't try to verify the listening Chrome's
        user_data_dir — if you point us at a port, we trust the port.
        """
        import time as _time

        from ai_dev_browser import (
            get_available_port,
            is_port_in_use,
            launch_chrome,
        )

        if requested_port is not None and is_port_in_use(port=requested_port) and not force_new:
            return requested_port, True

        port = requested_port if requested_port is not None else get_available_port(reuse=False)

        # If the requested port is taken by something we don't recognize and
        # caller asked for force_new, that's a hard error — don't silently
        # pick a different port.
        if requested_port is not None and is_port_in_use(port=requested_port):
            raise RuntimeError(
                f"Port {requested_port} is already in use. Cannot launch a "
                f"new Chrome on that port (force_new_chrome=True). Either "
                f"choose a different port or call browser_stop(port="
                f"{requested_port}) on whatever owns it."
            )

        process = launch_chrome(
            port=port,
            headless=headless,
            user_data_dir=str(user_data_dir),
            extra_args=extra_args,
        )

        # Wait for the debug port to bind. Mirror browser_start's polling
        # cadence so user-perceived behaviour is identical.
        deadline = _time.time() + startup_timeout
        while _time.time() < deadline:
            if is_port_in_use(port=port):
                # Briefly track the process handle so __aexit__ can decide
                # whether to leave it running (current default — same as
                # ai-dev-browser's "keep-alive after disconnect").
                self._chrome_process = process
                return port, False
            if process.poll() is not None:
                raise RuntimeError(
                    f"Chrome (PID {process.pid}) exited before binding port "
                    f"{port}. Likely causes: profile dir locked by another "
                    f"chrome.exe, insufficient permissions on {user_data_dir}, "
                    f"or Chrome rejected one of the launch flags."
                )
            _time.sleep(0.2)

        # Timed out — kill the process so its profile lockfile releases.
        import contextlib

        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                process.kill()
        raise RuntimeError(
            f"Chrome started (PID {process.pid}) but port {port} not "
            f"listening after {startup_timeout}s — process killed to "
            f"release profile lockfile. Retry with startup_timeout=<larger> "
            f"if your environment is slow (Windows + main Chrome running, "
            f"first-time profile init, AV scanning, etc.)."
        )

    async def __aenter__(self):
        import asyncio

        from ai_dev_browser import cdp
        from ai_dev_browser.core.connection import connect_browser

        # Ensure Chrome's stderr is redirected to DEVNULL (not PIPE).
        # Idempotent — installs once per process.
        _patch_ai_dev_browser_chrome_stderr()

        # Load cookies (deferred from __init__)
        if self._provided_cookies is not None:
            self.cookies = self._provided_cookies
        else:
            self.cookies = await self._load_or_setup_cookies()

        # Ensure Chrome is running (auto-launch if needed).
        #
        # We BYPASS ai-dev-browser's `browser_start` here. browser_start places
        # Chrome under `~/.ai-dev-browser/profiles/...` (the "managed
        # namespace"); any other agent in the same OS account that calls
        # `browser_cleanup()` will scan that namespace, probe each Chrome's
        # debug port, and `_kill_process_tree` anything whose probe fails to
        # come back alive. Our long-running Chrome under fanout load can
        # easily miss a single probe — friendly fire kills it. Reproduced by
        # an expert workflow on 2026-05.
        #
        # Workaround: launch Chrome ourselves with `--user-data-dir` pointing
        # OUTSIDE the managed namespace (default: ~/.grok-web-connector/
        # profiles/<profile>). `_is_managed_profile()` returns False for our
        # path → `browser_cleanup()` skips us → cross-agent safe by default.
        # Caller doesn't need to change anything.
        actual_port = self._remote_port  # Default to requested port
        if self._auto_launch:
            try:
                profile_name = self._profile or GROK_CHROME_PROFILE
                user_data_dir = self._user_data_dir or (
                    Path.home() / ".grok-web-connector" / "profiles" / profile_name
                )
                user_data_dir.mkdir(parents=True, exist_ok=True)

                actual_port, reused = self._launch_or_reuse_chrome(
                    user_data_dir=user_data_dir,
                    requested_port=self._remote_port,
                    headless=self._headless,
                    extra_args=[
                        "--disable-logging",
                        "--log-file=NUL",
                        *(self._extra_chrome_args or []),
                    ],
                    force_new=self._force_new_chrome,
                    startup_timeout=self._startup_timeout,
                )
                if reused:
                    logger.info(
                        f"Reusing Chrome on port {actual_port} "
                        f"(profile: {profile_name}, dir: {user_data_dir})"
                    )
                else:
                    logger.info(
                        f"Started new Chrome on port {actual_port} "
                        f"(profile: {profile_name}, dir: {user_data_dir})"
                    )
            except FileNotFoundError as e:
                raise GrokAPIError(str(e)) from e
            except (TimeoutError, RuntimeError) as e:
                # Chrome-start timeout remedies, ordered most-to-least
                # recoverable. Our floor is ai-dev-browser>=0.9.0 so
                # `startup_timeout` is always available to raise.
                raise GrokAPIError(
                    f"Chrome failed to start: {e}\n\n"
                    "Remedies (try in order):\n"
                    f"  1. Raise the port-bind timeout — current was "
                    f"{self._startup_timeout:.0f}s. Pass "
                    "startup_timeout=60 (or more) to GrokClient / "
                    "get_client(). Useful on slow/antivirus-heavy machines "
                    "or first-time profile init.\n"
                    "  2. Orphan chrome.exe processes may be locking the "
                    "profile dir. If Task Manager shows many chrome.exe "
                    "but netstat finds nothing on the debug port:\n"
                    "       taskkill /F /IM chrome.exe   (Windows)\n"
                    "       pkill -f chrome              (macOS/Linux)\n"
                    "     (ai-dev-browser 0.9.1+ will ship browser_cleanup() "
                    "as a safer, namespace-scoped alternative.)\n"
                    "  3. Reboot — reliably clears (1) and (2).\n"
                    "  4. If you repeatedly hit this on long-running "
                    "workers, pass close_chrome=True between sessions to "
                    "avoid orphan accumulation."
                ) from e

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

        # Inject a banner-killer that fires on every new document. Grok
        # serves a OneTrust cookie banner (id=onetrust-consent-sdk) as a
        # high-z-index bottom-right overlay; its hit-box covers the
        # 提交 submit button coords on typical desktop widths. CDP
        # Input.dispatchMouseEvent only sees hit-test results, not our
        # intended target, so the click is silently absorbed by the
        # banner — ProseMirror fills fine (DOM API, no coords) but
        # submit never triggers and create_image hangs to timeout.
        #
        # Kill the banner via Page.addScriptToEvaluateOnNewDocument so
        # it runs before React hydrates on every nav/reload, and via a
        # MutationObserver as a safety net if the banner remounts
        # asynchronously. Also run it once immediately in case we
        # reused a tab that already has the banner up.
        _banner_killer = r"""
        (() => {
            const MOD_TEXT_RE = /(moderat|inappropriate|policy|违反|违规|不当|审核|敏感)/i;
            const kill = () => {
                // OneTrust cookie banner — high-z overlay covering 提交 coords
                const ot = document.getElementById('onetrust-consent-sdk');
                if (ot) ot.remove();
                // Cookie/consent dialogs by text match
                document.querySelectorAll('[role="dialog"]').forEach(d => {
                    const t = (d.innerText || '');
                    if (t.includes('Cookie') || t.includes('隐私') || t.includes('同意')) {
                        d.remove();
                    }
                });
                // Moderation alerts/toasts. These persist across SPA
                // navigations after a moderated generation and can
                // overlap the 制作视频 / 提交 buttons on the next attempt,
                // causing create_video / create_image to silently fail
                // to find their click target on 4+ consecutive moderations.
                document.querySelectorAll('[role="alert"], [role="status"]').forEach(a => {
                    const t = (a.innerText || '');
                    if (MOD_TEXT_RE.test(t)) {
                        a.remove();
                    }
                });
            };
            kill();
            const start = () => {
                try {
                    const obs = new MutationObserver(kill);
                    obs.observe(document.body, {childList: true, subtree: true});
                } catch (e) { /* body not ready yet */ }
            };
            if (document.body) start();
            else document.addEventListener('DOMContentLoaded', start);
        })();
        """
        try:
            await self._tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=_banner_killer)
            )
        except Exception as _e:
            logger.debug(f"banner-killer inject skipped (non-fatal): {_e}")
        # Also run on the current document in case we reused a tab.
        import contextlib

        with contextlib.suppress(Exception):
            await self._tab.evaluate(_banner_killer)

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

        # Handle Cloudflare challenge if present.
        #
        # ai-dev-browser removed its built-in cloudflare_verify helper in
        # v0.8.0 (philosophy shift: "multimodal LLM + click primitive"
        # instead of heuristic wrappers). If the helper still exists
        # (<= 0.7.x), use it — most callers never see a CF challenge so
        # this is the smooth path. If the helper is gone, don't crash:
        # check for the challenge frame ourselves and surface it to the
        # caller so they can pass it manually and retry. Hard-failing the
        # entire get_client() on missing helper would break every caller
        # just because they upgraded ai-dev-browser.
        try:
            from ai_dev_browser.core.cloudflare import cloudflare_verify
        except ImportError:
            cloudflare_verify = None  # type: ignore[assignment]

        if cloudflare_verify is not None:
            result = await cloudflare_verify(self._tab, max_retries=15)
            if not result.get("verified"):
                raise GrokAuthError("Failed to bypass Cloudflare challenge")
        else:
            # Minimal detection: CF's Turnstile widget lives in an
            # iframe whose src contains "challenges.cloudflare.com".
            # If we find one, tell the caller — don't silently proceed,
            # downstream requests would just hang.
            cf_present = await self._tab.evaluate(
                r"""
                (() => {
                    const frames = document.querySelectorAll('iframe');
                    for (const f of frames) {
                        const src = f.getAttribute('src') || '';
                        if (src.includes('challenges.cloudflare.com')
                            || src.includes('/cdn-cgi/challenge-platform/')) {
                            return true;
                        }
                    }
                    // Heuristic text match for the full-page block page
                    const t = document.body ? document.body.innerText : '';
                    return /Verifying you are human|Cloudflare/i.test(t)
                        && /Checking your browser|需要验证|正在验证/.test(t);
                })()
                """
            )
            if cf_present:
                raise GrokAuthError(
                    "Cloudflare challenge detected on grok.com. The automation "
                    "Chrome can pass CF but Turnstile can take multiple tries "
                    "(CF scoring varies by IP / session freshness / load).\n\n"
                    "Remedies, in order:\n"
                    "  1. Click the Turnstile widget manually in the Chrome "
                    "window and give it 30-60s. If it keeps retrying, try a "
                    "different network or wait a few minutes — this is usually "
                    "transient.\n"
                    "  2. If (1) doesn't clear it (or you want to bypass the "
                    "widget entirely), paste a fresh cf_clearance from your "
                    "regular browser:\n"
                    "     a. Open grok.com in your normal Chrome/Firefox.\n"
                    "     b. DevTools → Application → Cookies → https://grok.com.\n"
                    "     c. Copy the cf_clearance Value column.\n"
                    "     d. Run:\n"
                    "          python -m grok_web.auth_manager refresh-cookies \\\n"
                    "              --cf-clearance '<paste_value>'\n"
                    "     e. Retry get_client(). sso / sso-rw / x-userid are kept.\n\n"
                    "  If sso / sso-rw also expired (check `python -m grok_web."
                    "auth_manager status`), run refresh-cookies without flags "
                    "for an interactive prompt to paste all four."
                )

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

    async def _wait_for_selector(
        self,
        selector: str,
        timeout: float = 30.0,
        interval: float = 0.25,
    ) -> bool:
        """Poll for a CSS selector to match a DOM element.

        Grok's Imagine panel hydrates on a lazy path — a fixed
        ``sleep(N)`` after navigation races the mount and intermittently
        returns null from ``querySelector``. Poll until the element
        appears or the timeout elapses. Returns ``True`` if found.
        """
        import asyncio

        escaped = selector.replace("\\", "\\\\").replace("'", "\\'")
        js = f"!!document.querySelector('{escaped}')"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                found = await self._tab.evaluate(js)
            except Exception:
                found = False
            if found:
                return True
            await asyncio.sleep(interval)
        return False

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

    async def _fetch_video_duration(self, video_id: str) -> tuple[int | None, float | None]:
        """Look up (duration_s, cumulative_duration_s) from Grok REST.

        Runs after create_video / extend_video returns so callers see
        authoritative length fields on the result objects without doing
        an extra GET themselves. Non-fatal on any lookup error —
        returns (None, None), caller treats that as "unknown".

        Cumulative = ``videoExtensionStartTime + videoDuration`` on the
        new video's post metadata. For a fresh create_video (no parent
        chain), videoExtensionStartTime is null and cumulative == duration.
        """
        try:
            d = await self.get_post_details(video_id)
        except Exception:
            return None, None
        rd = (d.raw_data or {}).get("post", d.raw_data or {})
        dur = rd.get("videoDuration")
        if dur is None:
            return None, None
        try:
            dur_int = int(dur)
        except (TypeError, ValueError):
            return None, None
        start = rd.get("videoExtensionStartTime")
        cumulative: float = float(start) + dur_int if start is not None else float(dur_int)
        return dur_int, cumulative

    async def _asset_request_head(self, asset_url: str) -> int:
        """Return Content-Length for an asset URL via a page-initiated
        HEAD request.

        Implementation: ``tab.evaluate("fetch(url, {method: 'HEAD'})")``
        inside the active grok.com tab. Grok serves video assets from
        two different hosts with different auth policies:

        * ``imagine-public.x.ai/imagine-public/share-videos/...`` —
          public CDN, returns ``Access-Control-Allow-Origin: *``,
          works with any credential mode.
        * ``assets.grok.com/users/<uid>/generated/...`` — authenticated
          signed URL, returns 403 unless the request includes the
          grok.com session cookie. Since it's a cross-origin fetch
          from the grok.com tab, the browser needs an explicit
          ``credentials: 'include'`` to attach cookies.

        We try the public-friendly mode first (cheap), fall back to
        ``include`` for signed URLs. Matches the same-pattern retry
        already used by ``_download_video_by_url`` in v0.13.7.

        Why this matters vs. the old CDP ``Network.loadNetworkResource``
        implementation:

        1. **No resource leak under batch load.**
           ``loadNetworkResource`` downloads the *full body* (several
           MB for a .mp4), holds a CDP network slot for the duration,
           and — critically — cannot be cancelled if the Python side
           times out. Accumulate ~200 such calls in a long-lived client
           and Chrome refuses new CDP WebSocket upgrades with HTTP 500.
           Page-context HEAD returns headers only and cleans up
           trivially via standard browser fetch lifecycle.
        2. **No Cloudflare fingerprint escalation.**
           CDP-injected fetches don't share the page's TLS fingerprint;
           repeated calls look bot-like and CF ramps challenge severity
           to the point that fresh-Chrome launches start failing. A
           page-initiated fetch on the real user origin is
           indistinguishable from a ``<video>`` tag's natural prefetch.
        3. **Works with ai-dev-browser >= 0.8.**
           The old path uses ``cdp.network.load_network_resource``;
           the new path uses only ``tab.evaluate``, so it has no
           dependency on CDP command surface stability.

        Callers that hit rate limits / slow CDN edges should see a
        clean ``GrokAPIError`` with elapsed time; HEAD requests don't
        hang Chrome even on a dead URL because fetch respects the
        ``AbortSignal.timeout`` we pass in.
        """
        import asyncio
        import json as _json

        PER_CALL_TIMEOUT_MS = 15000  # 15s per call; fetch aborts cleanly

        async def _try_head(credentials: str) -> dict:
            """Run one HEAD with the given credentials mode. Returns
            ``{ok, status, length, type}`` on a network-level success
            (including 4xx/5xx) or ``{ok: False, error}`` on fetch
            abort / DOMException.
            """
            js = r"""
                (async () => {
                    try {
                        const r = await fetch(__URL__, {
                            method: 'HEAD',
                            credentials: __CREDS__,
                            signal: AbortSignal.timeout(__TIMEOUT__),
                        });
                        return JSON.stringify({
                            ok: true,
                            status: r.status,
                            length: r.headers.get('content-length'),
                            type: r.headers.get('content-type'),
                        });
                    } catch (e) {
                        return JSON.stringify({
                            ok: false,
                            error: (e && e.name) ? e.name + ': ' + e.message : String(e),
                        });
                    }
                })()
            """
            js = (
                js.replace("__URL__", _json.dumps(asset_url))
                .replace("__CREDS__", _json.dumps(credentials))
                .replace("__TIMEOUT__", str(PER_CALL_TIMEOUT_MS))
            )
            raw = await self._tab.evaluate(js, await_promise=True)
            return _json.loads(raw) if isinstance(raw, str) else (raw or {})

        t0 = asyncio.get_event_loop().time()
        try:
            # Strategy 1: default-ish credentials (omit). Fast path for
            # imagine-public.x.ai which returns ACAO:* and doesn't
            # care about cookies.
            data = await _try_head("omit")

            # Strategy 2: if the CDN said 403/401, retry with
            # credentials:'include' so cookies flow cross-origin. This
            # is the path assets.grok.com's signed URLs need.
            if isinstance(data, dict) and data.get("ok") and data.get("status") in (401, 403):
                logger.debug(
                    "[asset_head] %s returned %s under omit; retrying with credentials=include",
                    asset_url[:80],
                    data.get("status"),
                )
                data2 = await _try_head("include")
                if isinstance(data2, dict) and data2.get("ok"):
                    data = data2

            if not data or not data.get("ok"):
                err = (data or {}).get("error", "unknown") if isinstance(data, dict) else "unknown"
                elapsed = asyncio.get_event_loop().time() - t0
                raise GrokAPIError(
                    f"Page-context HEAD failed after {elapsed:.1f}s. URL: {asset_url}, Error: {err}"
                )

            status = data.get("status")
            if status == 403:
                raise GrokAuthError(
                    f"Asset access denied (403) after trying both omit + "
                    f"include credential modes. URL: {asset_url}. If this "
                    f"just started happening, your grok.com session cookies "
                    f"may have expired — sign out and back in on the web UI, "
                    f"then retry."
                )
            if status and status >= 400:
                raise GrokAPIError(f"Asset request failed: HTTP {status}")

            length = data.get("length")
            if length is None:
                raise GrokAPIError(f"No Content-Length header in HEAD response for {asset_url}")
            try:
                return int(length)
            except (TypeError, ValueError) as e:
                raise GrokAPIError(f"Non-integer Content-Length {length!r} for {asset_url}") from e
        except (GrokAPIError, GrokAuthError):
            raise
        except Exception as e:  # noqa: BLE001
            elapsed = asyncio.get_event_loop().time() - t0
            raise GrokAPIError(
                f"Page-context HEAD failed after {elapsed:.1f}s. URL: {asset_url}, Error: {e}"
            ) from e

    # =========================================================================
    # API Methods (business logic using the I/O primitives above)
    # =========================================================================

    async def list_posts(
        self,
        limit: int | None = 40,
        source: str | None = "favorites",
        include_raw_data: bool = False,
        safe_for_work: bool = True,
    ) -> list[PostSummary]:
        """List posts with basic metadata, with automatic pagination.

        Args:
            limit: Maximum number of posts to return, or None for all.
                Pagination is handled automatically via cursor.
            source: Filter by source type:
                - "favorites": Your saved/favorited posts (default)
                - None: All public posts
            include_raw_data: Include raw API response in each PostSummary
            safe_for_work: When False, sets Grok's ``safeForWork: false``
                filter parameter to match what the Grok web UI sends by
                default. Empirically this does NOT broaden the result
                set vs ``safe_for_work=True`` for the
                ``source="favorites"`` filter — both return identical
                lists. Exposed so the connector matches UI request
                shape, and in case future Grok-side filter behavior
                differentiates them. Don't rely on this to unlock
                enumeration of NSFW user-generated content — that
                channel hasn't been found.
        """
        # Map user-friendly source names to API values
        api_source = source
        if source == "favorites":
            api_source = "MEDIA_POST_SOURCE_LIKED"

        filter_data: dict[str, Any] = {}
        if api_source:
            filter_data["source"] = api_source
        if not safe_for_work:
            # Match the UI's default for non-SFW environments. Grok's
            # bulk listing filters NSFW out unless this is explicitly
            # false. Without it, user-generated NSFW content is invisible
            # via this enumeration even if it's the user's own posts.
            filter_data["safeForWork"] = False

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

    async def wait_for_video_completion(
        self, params, *legacy_args, **legacy_kwargs
    ) -> VideoGenerationResult:
        """Poll Grok's REST until an in-flight video finishes rendering.

        Canonical shape (v0.19.0+) — dict-style::

            await client.wait_for_video_completion({
                "video_id": "abc-123",
                "timeout": 600,
            })

        Use this when :meth:`create_video` or :meth:`extend_video`
        returned a result with ``in_progress == True`` — i.e. the local
        polling timeout fired before Grok finished, but the video is
        still being generated on the server. Avoids re-submitting the
        whole job (which would burn another queue slot + statsig quota).

        Polls ``/rest/media/post/get`` every ``poll_interval`` seconds,
        treating a populated ``mediaUrl`` without the moderated-placeholder
        thumbnail as success.

        Args:
            params: Dict with keys from WAIT_FOR_COMPLETION_KEYS (see
                grok_web.schema). ``video_id`` is required. Per-key
                descriptions below are generated from
                ``grok_web.schema.PARAMS`` (SSOT).

                <SCHEMA_ARGS>

        Returns:
            A synthesized ``VideoGenerationResult`` with ``progress=100``
            and the authoritative ``duration_s`` / ``parent_post_id`` /
            ``source_post_id`` from the finished post.

        Raises:
            GrokAPIError: If Grok moderated the video post-render, or if
                polling exceeded ``timeout`` before completion.
            TypeError: If ``params`` is not a dict (or, for the
                deprecated positional form, a ``video_id`` string), or
                if ``video_id`` is missing.

        Example:
            >>> result = await client.create_video({"images": [...], "prompt": "..."})
            >>> if result.in_progress:
            ...     # Server was still generating when we gave up — wait it out.
            ...     result = await client.wait_for_video_completion({
            ...         "video_id": result.video_id, "timeout": 600,
            ...     })
            >>> assert result.is_complete and not result.moderated

        Legacy form (deprecated v0.19.0, removed v0.20.0)::

            await client.wait_for_video_completion(video_id, timeout=600)

        Still works but emits ``DeprecationWarning``.
        """
        import warnings

        from .schema import WAIT_FOR_COMPLETION_KEYS, validate_params

        if isinstance(params, str):
            warnings.warn(
                "wait_for_video_completion(video_id, ...) positional/kwarg "
                "form is deprecated since v0.19.0; use "
                "wait_for_video_completion({'video_id': ..., ...}) for "
                "consistency with create_video / edit_image. Removed in "
                "v0.20.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            normalized = {"video_id": params}
            # Accept legacy positional timeout/poll_interval as well.
            legacy_positional_keys = ("timeout", "poll_interval")
            for i, val in enumerate(legacy_args):
                if i >= len(legacy_positional_keys):
                    raise TypeError(
                        "wait_for_video_completion: too many positional args "
                        "(legacy signature accepts at most 3, got "
                        f"{1 + len(legacy_args)})"
                    )
                normalized[legacy_positional_keys[i]] = val
            normalized.update(legacy_kwargs)
            params = normalized
        elif isinstance(params, dict):
            if legacy_args or legacy_kwargs:
                raise TypeError(
                    "wait_for_video_completion: cannot mix dict params and "
                    "positional/kwargs. Pass everything inside the dict."
                )
        else:
            raise TypeError(
                "wait_for_video_completion: first arg must be a dict "
                "(canonical) or a video_id str (deprecated), got "
                f"{type(params).__name__}"
            )

        p = validate_params(params, WAIT_FOR_COMPLETION_KEYS)
        video_id = p.get("video_id")
        if not video_id:
            raise TypeError("wait_for_video_completion: 'video_id' is required in params dict")
        timeout = p.get("timeout", 300)
        poll_interval = p.get("poll_interval", 5.0)

        import asyncio as _asyncio

        deadline = _asyncio.get_event_loop().time() + timeout
        last_post: dict = {}
        while _asyncio.get_event_loop().time() < deadline:
            try:
                data = await self._api_request("POST", MEDIA_POST_GET_ENDPOINT, {"id": video_id})
            except GrokNotFoundError:
                raise GrokAPIError(
                    f"wait_for_video_completion: video {video_id} not found "
                    "(deleted / wrong id / not yet persisted)"
                ) from None
            except Exception as e:
                logger.debug(f"wait_for_video_completion: poll error, retrying: {e}")
                await _asyncio.sleep(poll_interval)
                continue

            post = data.get("post", data)
            last_post = post
            thumb = post.get("thumbnailImageUrl") or ""
            if MODERATED_THUMBNAIL_UUID in thumb:
                raise GrokAPIError(
                    f"Video {video_id} was moderated post-render "
                    "(thumbnail matches moderated-placeholder UUID)."
                )
            if post.get("mediaUrl"):
                # Populated media URL = render complete, not moderated.
                duration_s = post.get("videoDuration")
                try:
                    duration_s_int = int(duration_s) if duration_s is not None else None
                except (TypeError, ValueError):
                    duration_s_int = None
                parent = post.get("originalPostId") or video_id
                return VideoGenerationResult(
                    video_id=video_id,
                    source_post_id=parent,
                    parent_post_id=parent,
                    moderated=False,
                    progress=100,
                    mode=post.get("mode") or "normal",
                    model_name=post.get("modelName"),
                    image_reference=None,
                    conversation_id=None,
                    statsig_id=None,
                    duration_s=duration_s_int,
                )
            await _asyncio.sleep(poll_interval)

        # Timed out. Report best-known state.
        raise GrokAPIError(
            f"wait_for_video_completion: video {video_id} did not finish "
            f"within {timeout}s (last seen: mediaUrl="
            f"{last_post.get('mediaUrl')!r}). Still in flight on Grok's "
            "side; either call wait_for_video_completion again with a "
            "larger timeout, or re-check later via get_post_details."
        )

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

    async def _download_url_to_file(self, url: str, dest: Path) -> bool:
        """Download a URL via page-context fetch and write bytes to ``dest``.

        Two-strategy fallback (mirrors :meth:`_asset_request_head`):
        ``credentials: 'omit'`` first — works for public CDN URLs
        (imagine-public.x.ai serves ACAO:* which CORS rejects with
        credentials:include). On 401/403 falls back to
        ``credentials: 'include'`` for signed paths (assets.grok.com).

        Used by :meth:`_resolve_image_refs_to_local` to download
        ``post:<uuid>`` reference images for re-upload via the file
        input on the Imagine homepage / Custom edit panel.
        """
        import base64
        import json as _json

        async def _fetch(creds: str) -> dict:
            js = """
                (async () => {
                    try {
                        const r = await fetch(__URL__, {credentials: __CREDS__});
                        if (!r.ok) return JSON.stringify({ok: false, status: r.status});
                        const buf = await r.arrayBuffer();
                        const u8 = new Uint8Array(buf);
                        let bin = '';
                        for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
                        return JSON.stringify({ok: true, b64: btoa(bin), size: u8.length});
                    } catch (e) {
                        return JSON.stringify({ok: false, error: String(e)});
                    }
                })()
            """.replace("__URL__", _json.dumps(url)).replace("__CREDS__", _json.dumps(creds))
            raw = await self._tab.evaluate(js, await_promise=True)
            try:
                return _json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                return {"ok": False}

        obj = await _fetch("omit")
        if not obj.get("ok") and obj.get("status") in (401, 403):
            obj = await _fetch("include")
        if not obj.get("ok"):
            return False
        try:
            data = base64.b64decode(obj["b64"])
            # Same flush+fsync as _download_video_by_url — the immediate
            # consumer here is the browser's file input via CDP, which
            # also opens the file out-of-process and could see a partial
            # state on Windows.
            import os as _os

            with open(dest, "wb") as _f:
                _f.write(data)
                _f.flush()
                _os.fsync(_f.fileno())
            if dest.stat().st_size != len(data):
                return False
        except Exception:
            return False
        return True

    async def _resolve_image_refs_to_local(self, images: list[str], tmpdir: Path) -> list[Path]:
        """Resolve a mixed-form ``images`` list to local file paths.

        Each entry is one of:
        - bare path / ``"path:..."``  → used as-is, must exist on disk
        - ``"post:<uuid>"``           → downloads the post's media via
                                        :meth:`get_post_details` ``media_url``,
                                        writes to ``tmpdir`` (caller cleans up)
        - ``"file:<id>"``             → not supported here (would need an
                                        upload-vs-fetch path); raises GrokAPIError
        - ``"video:<uuid>"``          → not supported (videos can't be image refs)

        Returns the resolved list of :class:`Path` objects in input order.
        """
        from .prompt_parser import classify_image_source

        resolved: list[Path] = []
        for spec in images:
            kind, value = classify_image_source(spec)
            if kind == "file":
                p = Path(value)
                if not p.exists():
                    raise GrokAPIError(f"Image ref not found on disk: {value}")
                resolved.append(p)
                continue
            if kind == "post":
                try:
                    details = await self.get_post_details(value)
                except Exception as e:
                    raise GrokAPIError(
                        f"Resolve post:{value} as image ref: get_post_details failed: {e}"
                    ) from e
                url = details.media_url or details.thumbnail_url
                if not url:
                    raise GrokAPIError(
                        f"post:{value} has no media_url — cannot use as image ref. "
                        "Either it's a video post (use the thumbnail explicitly) or "
                        "the post has no media."
                    )
                # Pick a file extension from the URL; default .jpg.
                suffix = ".jpg"
                for ext in (".png", ".jpg", ".jpeg", ".webp"):
                    if ext in url.lower():
                        suffix = ext
                        break
                dest = tmpdir / f"ref_{value}{suffix}"
                if not await self._download_url_to_file(url, dest):
                    raise GrokAPIError(f"Failed to download post:{value}'s media from {url}")
                resolved.append(dest)
                continue
            if kind == "video":
                raise GrokAPIError(
                    f"video:{value} is not a valid image reference. Image refs "
                    "must be image posts (post:<uuid>) or local files."
                )
            if kind == "upload":
                raise GrokAPIError(
                    f"file:{value} (previously uploaded ID) is not supported as an "
                    "image ref for create_image / edit_image — these flows use "
                    "the UI file input, not the REST upload endpoint. Use "
                    "post:<uuid> or a local path instead."
                )
            raise GrokAPIError(f"Unrecognized image ref form: {spec!r}")
        return resolved

    async def _scan_for_rate_limit_banner(self) -> tuple[str, str] | None:
        """Scan visible toasts / alerts for Grok rate-limit or quota text.

        Grok's UI surfaces rate-limiting and quota-exhaustion via
        transient toast banners in ``[role="status"]`` /
        ``[role="alert"]`` / ``[aria-live]`` regions. The underlying
        NDJSON response in these cases OFTEN comes back with
        ``moderated=true`` and NO rate-limit error code — so without a
        DOM-level signal, callers see a flood of "moderated" results
        and burn their retry budget on what's actually throttling.

        Returns ``(category, text)`` where category is
        ``"quota_exceeded"`` (hard stop, don't retry today) or
        ``"rate_limit"`` (transient, backoff + retry), or ``None`` if no
        matching banner is currently visible.
        """
        # JS returns JSON.stringify-ed dict (or "null"). Plain-dict returns
        # from tab.evaluate come back as a CDP RemoteObject preview shape
        # ([[key, {type,value}], ...]) which is awkward to parse — stringifying
        # in JS and loading in Python is the sturdy path.
        raw = await self._tab.evaluate(
            r"""
            JSON.stringify((() => {
                const QUOTA_RE = /(daily\s*limit|quota\s*exceeded|quota\s*used|今日.*上限|已达.*上限|用尽|额度.*耗尽|daily\s*quota)/i;
                const RATE_RE = /(rate\s*limit|too\s*many|try\s*again\s*later|slow\s*down|请稍后|稍候|频繁|限流|限制|throttl)/i;
                const nodes = document.querySelectorAll('[role="status"], [role="alert"], [aria-live]');
                for (const n of nodes) {
                    const r = n.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const t = (n.innerText || '').trim();
                    if (!t) continue;
                    if (QUOTA_RE.test(t)) return {category: 'quota_exceeded', text: t.substring(0, 240)};
                    if (RATE_RE.test(t)) return {category: 'rate_limit', text: t.substring(0, 240)};
                }
                return null;
            })())
            """,
            await_promise=False,
        )
        if not isinstance(raw, str) or raw == "null":
            return None
        import json as _json

        try:
            parsed = _json.loads(raw)
        except Exception:
            return None
        if isinstance(parsed, dict) and parsed.get("category"):
            return parsed["category"], parsed.get("text", "")
        return None

    async def _get_favorited_state(self, post_id: str) -> bool | None:
        """Best-effort detection of whether a post is in the user's favorites.

        Uses ``get_post_details`` and inspects ``userInteractionStatus``
        for any of the known "favorited"/"saved"/"liked" boolean keys.
        Returns ``None`` when the field is missing or has an unrecognised
        shape — callers that use this to drive account-state mutations
        MUST treat ``None`` as "don't touch" rather than guess, since a
        false-negative would cause the connector to silently remove a
        favorite the user placed themselves.
        """
        try:
            details = await self.get_post_details(post_id)
        except Exception:
            return None
        raw = (details.raw_data or {}).get("post", details.raw_data or {})
        uis = raw.get("userInteractionStatus")
        if isinstance(uis, dict):
            for key in ("favorited", "saved", "liked", "isFavorited", "isSaved"):
                if key in uis and isinstance(uis[key], bool):
                    return uis[key]
        if isinstance(uis, bool):
            return uis
        return None

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
            f"Video not found in all favorites.\nVideo ID: {video_id}\nLocal file: {filename}\n"
        )

    async def _match_by_file_size_via_favorites(
        self,
        local_size: int,
        filename: str,
        hint_uuid: str | None = None,
        max_posts: int | None = None,
    ) -> VideoMatchResult:
        """Search all liked posts to find video by file size.

        Bail-fast: if HEAD timeouts pile up (Chrome's network slots are
        saturated from prior slow downloads), we stop the scan and
        surface a clear error rather than grinding through 50+ more
        URLs that will all time out.
        """
        posts = await self.list_posts(limit=max_posts, source="MEDIA_POST_SOURCE_LIKED")

        MAX_CONSECUTIVE_TIMEOUTS = 3  # scan-wide — videos + posts combined
        consecutive_timeouts = 0

        def _is_timeout_err(err: BaseException) -> bool:
            text = str(err).lower()
            return "timed out" in text or "timeout" in text

        for post_summary in posts:
            try:
                details = await self.get_post_details(post_summary.id)

                # Check parent video (for txt2vid posts)
                if details.mode == MODE_TXT2VID and details.hd_media_url:
                    try:
                        web_size = await self.get_asset_file_size(details.hd_media_url)
                        consecutive_timeouts = 0
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
                    except GrokAPIError as e:
                        if _is_timeout_err(e):
                            consecutive_timeouts += 1
                    except Exception:
                        pass

                # Check all children
                for child in details.children:
                    url = child.hd_media_url or child.media_url
                    if not url:
                        continue

                    try:
                        web_size = await self.get_asset_file_size(url)
                        consecutive_timeouts = 0
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
                    except GrokAPIError as e:
                        if _is_timeout_err(e):
                            consecutive_timeouts += 1
                            if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                                raise GrokAPIError(
                                    f"Favorites scan aborted after "
                                    f"{consecutive_timeouts} consecutive HEAD "
                                    f"timeouts — Chrome's network slots are "
                                    f"saturated. Re-enter get_client() to "
                                    f"reset state and retry. Local file: "
                                    f"{filename}, size {local_size} bytes."
                                ) from e
                    except Exception:
                        continue

            except GrokAPIError:
                # Already-structured errors (e.g. the bail-fast above)
                # should propagate.
                raise
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

    async def select_post(self, params, **legacy_kwargs) -> None:
        """Select a specific post on the Imagine tab.

        Canonical shape (v0.19.0+) — dict-style::

            await client.select_post({
                "post_id": "abc-123",
                "timeout": 6.0,
            })

        This is a correctness-guaranteed navigation primitive: a plain
        ``/imagine/post/<id>`` fetch is not sufficient, because Grok's
        SPA frequently redirects to the chain's "latest" node (most
        recent video / edit) when the requested node has descendants.
        Leaving that redirect uncorrected makes every subsequent UI
        action operate on the wrong post — the classic
        ``extend_video({'video_id': X})`` fanout trap, a
        ``create_video({'images': ['post:X']})`` where the source
        image is an old root with video children, ``edit_image`` on
        deep chains, etc.

        Resolution: navigate, wait for hydration, check the URL/DOM,
        and — if the redirect happened — click the sidebar thumbnail
        whose ``<img>`` src references ``post_id``. Thumbnails encode
        their post_id in the media URL so matching is reliable.

        Compose this with :meth:`extend_current`,
        :meth:`generate_video_from_current`, :meth:`edit_current` to
        build end-to-end flows where the target post is explicit.

        Args:
            params: Dict with keys from SELECT_POST_KEYS (see
                grok_web.schema). ``post_id`` is required; ``timeout``
                defaults to 6.0. Per-key descriptions below are
                generated from ``grok_web.schema.PARAMS`` (SSOT).

                <SCHEMA_ARGS>

        Raises:
            GrokAPIError: Post is 404, or the thumbnail-correction
                failed to find a sidebar entry matching ``post_id``
                within ``timeout`` (most often: the post doesn't
                belong to a chain Grok will show in the sidebar —
                e.g. orphaned / deleted).
            TypeError: If ``params`` is not a dict (or, for the
                deprecated positional form, a ``post_id`` string), or
                if ``post_id`` is missing.

        Legacy form (deprecated v0.19.0, removed v0.20.0)::

            await client.select_post("abc-123", timeout=6.0)

        Still works but emits ``DeprecationWarning``.
        """
        import warnings

        from .schema import SELECT_POST_KEYS, validate_params

        if isinstance(params, str):
            warnings.warn(
                "select_post(post_id, ...) positional/kwarg form is "
                "deprecated since v0.19.0; use select_post({'post_id': ...}) "
                "for consistency with create_video / edit_image. Removed "
                "in v0.20.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            params = {"post_id": params, **legacy_kwargs}
        elif isinstance(params, dict):
            if legacy_kwargs:
                raise TypeError(
                    "select_post: cannot mix dict params and kwargs. "
                    "Pass everything inside the dict."
                )
        else:
            raise TypeError(
                f"select_post: first arg must be a dict (canonical) or "
                f"a post_id str (deprecated), got {type(params).__name__}"
            )

        p = validate_params(params, SELECT_POST_KEYS)
        post_id = p.get("post_id")
        if not post_id:
            raise TypeError("select_post: 'post_id' is required in params dict")
        # select_post defaults to 6.0s — short, since the caller pays
        # the page-load latency before this is checked. Read from the
        # original params so the schema's int(300) default doesn't win.
        timeout = params.get("timeout", 6.0)

        import asyncio

        url = f"{self.BASE_URL}/imagine/post/{post_id}"
        await self._tab.get(url)
        # Initial hydration wait. Keep short; we'll poll after.
        await asyncio.sleep(2)

        # 404 check (Grok renders the SPA shell with a "not found" body).
        page_text = await self._tab.evaluate("document.body.innerText")
        if page_text and ("Page not found" in page_text or "404" in page_text):
            raise GrokAPIError(f"Post {post_id} not found (404)")

        # Fast-path: URL still contains the requested id. Common for
        # root nodes / chain tails where Grok doesn't redirect.
        current_url = await self._tab.evaluate("location.href")
        if post_id in str(current_url):
            return

        # Redirect path: Grok's SPA jumped us to a descendant. The
        # sidebar (left-column post list) shows every node in the
        # chain as a clickable thumbnail with <img src> containing
        # the node's own uuid. Find the one for post_id and click it.
        logger.debug(
            "[select_post] URL redirected (%s → %s); clicking sidebar "
            "thumbnail to restore selection",
            post_id,
            current_url,
        )
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            clicked = await self._tab.evaluate(
                r"""
                (() => {
                    const want = "__POST_ID__";
                    const imgs = Array.from(document.querySelectorAll('img'))
                        .filter(i => (i.currentSrc || i.src || '').includes(want));
                    if (imgs.length === 0) return 'no-thumb';
                    // Walk up to the nearest clickable (BUTTON or A).
                    let el = imgs[0];
                    while (el && el.tagName !== 'BUTTON' && el.tagName !== 'A'
                           && el.parentElement) {
                        el = el.parentElement;
                    }
                    if (!el) return 'no-button';
                    const r = el.getBoundingClientRect();
                    const x = r.x + r.width/2, y = r.y + r.height/2;
                    const o = {bubbles:true, cancelable:true, clientX:x, clientY:y,
                               pointerType:'mouse', button:0, pointerId:1, isPrimary:true};
                    el.dispatchEvent(new PointerEvent('pointerdown', o));
                    el.dispatchEvent(new MouseEvent('mousedown', o));
                    el.dispatchEvent(new PointerEvent('pointerup', o));
                    el.dispatchEvent(new MouseEvent('mouseup', o));
                    el.dispatchEvent(new MouseEvent('click', o));
                    return 'ok';
                })()
                """.replace("__POST_ID__", post_id)
            )
            if clicked == "ok":
                await asyncio.sleep(1.0)
                # Verify: URL should now contain post_id OR the main
                # <img>/<video> should reference it. Accept either.
                current_url = await self._tab.evaluate("location.href")
                if post_id in str(current_url):
                    return
                landed = await self._tab.evaluate(
                    r"""
                    (() => {
                        const want = "__POST_ID__";
                        // The post "main view" is typically the largest
                        // <img> or <video> near the top-right of the layout.
                        const mains = Array.from(document.querySelectorAll('img, video'))
                            .filter(e => {
                                const r = e.getBoundingClientRect();
                                return r.width > 200;
                            })
                            .map(e => e.currentSrc || e.src || '')
                            .filter(Boolean);
                        return mains.some(src => src.includes(want));
                    })()
                    """.replace("__POST_ID__", post_id)
                )
                if landed:
                    return
            # Not yet. Retry the loop (sidebar may still be hydrating).
            await asyncio.sleep(0.5)

        raise GrokAPIError(
            f"select_post: could not select {post_id} within {timeout}s. "
            "The sidebar didn't surface a thumbnail matching this id — "
            "the post may not be part of a chain Grok renders in the "
            "left column (orphan, deleted, or different account)."
        )

    async def _navigate_to_post(self, post_id: str) -> None:
        """Deprecated: use :meth:`select_post`.

        Thin alias kept for internal callers; new code should use
        ``select_post`` which guarantees the requested post is
        actually selected even when Grok's SPA redirects.
        """
        await self.select_post({"post_id": post_id})

    # =========================================================================
    # UI Menu Operations (shared helper + specific actions)
    # =========================================================================

    async def _open_post_menu(self, post_id: str) -> bool:
        """
        Navigate to a post and open its "..." menu.

        Thin wrapper: navigate + 404 check, then delegate to the
        resilient :func:`actions.post_menu.open_post_menu` (which handles
        the multi-locale + structural-fallback locator strategy and
        retries 3x). This is the shared helper for favorite / unfavorite
        / delete / like / dislike etc.

        Args:
            post_id: The post UUID to navigate to

        Returns:
            True if menu was opened successfully

        Raises:
            GrokAPIError: If post is 404 or menu button not found
        """
        import asyncio

        from .actions.post_menu import open_post_menu

        d = self._ui_delay

        # Navigate to the post page
        await self._tab.get(f"{self.BASE_URL}/imagine/post/{post_id}")
        await asyncio.sleep(3 * d)

        # Check if page is 404
        page_text = await self._tab.evaluate("document.body.innerText")
        if "Page not found" in page_text or "404" in page_text:
            raise GrokAPIError(f"Post {post_id} not found (404)")

        return await open_post_menu(self._tab, delay=d)

    async def _click_menu_item(self, *text_options: str) -> bool:
        """
        Click a menu item matching any of the given text_options.

        Matches against both the item's trimmed ``innerText`` AND its
        ``aria-label``. Some 2026-04 Grok menuitems (e.g. 赞 / 踩 at the
        top of the "..." menu) are rendered as icon-only buttons with
        an empty innerText and the Chinese label on aria-label — so
        text-only matching silently missed them and raised "menu item
        not found" even though the item was present.

        Args:
            *text_options: One or more strings to match (e.g., "Save", "保存")

        Returns:
            True if item was clicked

        Raises:
            GrokAPIError: If menu item not found
        """
        import asyncio

        d = self._ui_delay

        for _ in range(3):
            items = await self._tab.query_selector_all('[role="menuitem"]')

            for item in items:
                item_text = item.text.strip() if item.text else ""
                aria_label = item.attrs.get("aria-label", "") if hasattr(item, "attrs") else ""
                if not aria_label:
                    # Fallback: read aria-label via evaluate
                    try:
                        idx = items.index(item)
                        aria_label = (
                            await self._tab.evaluate(
                                f"document.querySelectorAll('[role=\"menuitem\"]')"
                                f"[{idx}].getAttribute('aria-label') || ''"
                            )
                            or ""
                        )
                    except Exception:
                        aria_label = ""

                if item_text in text_options or aria_label in text_options:
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

    async def extend_current(
        self,
        *,
        seed_start: float | None = None,
        duration: str | None = None,
        prompt: str | None = None,
        timeout: int = 600,
        video_duration_hint: float | None = None,
    ) -> VideoGenerationResult:
        """Extend whatever post is currently selected on the tab.

        Lower-level primitive: opens "..." → 扩展, configures the
        filmstrip (duration + optional seed drag), fills the prompt,
        submits. Does NOT navigate. Caller is responsible for ensuring
        the correct post is selected via :meth:`select_post` first —
        otherwise extend operates on whatever Grok happens to be
        showing, which is the classic "extend silently serializes the
        chain" trap.

        When ``seed_start`` is None we skip the drag entirely; the
        filmstrip's native default handle position is the currently
        selected post's own tail, which is what a freshly-selected
        node's default state resolves to. Pass an explicit
        ``seed_start`` only for surgical mid-source seeding.

        See :meth:`extend_video` for the video_id-driven wrapper that
        composes ``select_post + extend_current`` — use that for the
        common case; use ``extend_current`` directly when composing
        with your own navigation logic.

        Args:
            seed_start: Specific seed timestamp in chain-coords; None
                uses the filmstrip's native default (selected post's
                own tail).
            duration: '6s' or '10s'. None keeps Grok's current toggle.
            prompt: Extend prompt. None leaves the UI's pre-filled text.
            timeout: Max seconds to wait for the generation response.
            video_duration_hint: Optional filmstrip-timeline length in
                seconds (equal to ``videoExtensionStartTime +
                videoDuration`` on the selected post). Skips a DOM
                bootstrap inside ``wait_for_filmstrip``. If omitted we
                bootstrap from the DOM.

        Returns:
            VideoGenerationResult. For full source-linkage metadata
            (source_video_id, seed_start_*, duration_s,
            cumulative_duration_s), use :meth:`extend_video`.
        """
        import asyncio
        import random

        from .actions.extend_seed import (
            SEED_DRIFT_TOLERANCE,
            click_generate,
            drag_seed_handle,
            enable_focus_emulation,
            fill_prompt,
            read_actual_seed_start,
            select_duration,
            wait_for_filmstrip,
        )
        from .actions.network_monitor import CDPMonitor
        from .actions.post_menu import click_menu_item, open_post_menu

        if duration is not None and duration not in {"6s", "10s"}:
            logger.warning(
                f"Unknown duration {duration!r} (known: '6s', '10s'); "
                "passing through — Grok UI may reject."
            )

        # Focus emulation so CDP press/release events don't get dropped
        # when the window lacks OS focus.
        await enable_focus_emulation(self._tab)

        async with CDPMonitor(self._tab, "/app-chat/conversations/new") as monitor:
            await asyncio.sleep(1 + random.uniform(0, 0.5))

            # 1. Open "..." and click 扩展. Keep legacy label names as
            # fallbacks in case Grok reverts. prefer_media="video" disambiguates
            # on chain-root posts where both image and video cards render
            # their own '...' triggers — extend wants the video-context one.
            await open_post_menu(self._tab, delay=self._ui_delay, prefer_media="video")
            await click_menu_item(
                self._tab,
                "扩展",
                "扩展视频",
                "延长视频",
                "Extend",
                "Extend video",
                "Extend Video",
                delay=self._ui_delay,
            )

            # 2. Wait for the filmstrip to mount.
            fs = await wait_for_filmstrip(
                self._tab, timeout=8.0, video_duration_hint=video_duration_hint
            )
            video_duration = fs["video_duration"]

            # 3. Duration toggle (must precede any drag — changing
            # duration resets the selection window).
            if duration is not None:
                await select_duration(self._tab, duration)
                fs = await wait_for_filmstrip(
                    self._tab, timeout=3.0, video_duration_hint=video_duration_hint
                )

            # 4. Drag only if caller asked for a specific seed — the
            # native handle already sits at the selected post's tail.
            actual_seed: float | None = None
            displayed_seed: int | None = None
            if seed_start is not None:
                await drag_seed_handle(
                    self._tab,
                    filmstrip_rect=fs["filmstrip_rect"],
                    handle_rect=fs["handle_rect"],
                    video_duration=video_duration,
                    seed_start=seed_start,
                )
                readback = await read_actual_seed_start(self._tab, video_duration=video_duration)
                actual_seed = readback["actual"]
                displayed_seed = readback["displayed"]
                if actual_seed is not None and abs(actual_seed - seed_start) > SEED_DRIFT_TOLERANCE:
                    raise GrokAPIError(
                        f"Seed drag drifted: requested {seed_start:.2f}s, "
                        f"landed at {actual_seed:.2f}s (tolerance "
                        f"{SEED_DRIFT_TOLERANCE}s). Filmstrip DOM may not "
                        "be stable; retry."
                    )

            # 5. Prompt
            if prompt is not None:
                await fill_prompt(self._tab, prompt)

            # 6. Submit
            await click_generate(self._tab)

            if not await monitor.wait_for_request(timeout=10):
                raise GrokAPIError(
                    "Extend did not trigger a generation request after "
                    "clicking 生成视频. The button may still be disabled "
                    "(missing prompt? seed not selected?) or the UI changed. "
                    "Use get_menu_items() / screenshots to debug."
                )
            await monitor.wait_for_body(timeout=timeout)

        # Parse response. source_post_id on the base result defaults to
        # parentPostId from NDJSON; the wrapper (extend_video) will
        # override with the caller-provided source_video_id.
        gen_result = parse_video_ndjson_response(
            monitor.body, parent_post_id="", statsig_id=monitor.statsig_id
        )

        # Rate-limit / quota reclassification. Grok's NDJSON frequently
        # reports moderated=true with no rate-limit error code when the
        # actual failure is anti-abuse throttle or daily quota — and the
        # UI surfaces a toast banner. A rate-limit / quota signal is
        # strictly more severe than moderation (affects the whole
        # session, not one prompt), so raise regardless of the parsed
        # moderated state: even if the current generation happened to
        # produce a real video, the caller needs to know the session is
        # now throttled so they can back off. False-positive risk
        # (stale toast from an earlier call while this one succeeded)
        # exists but is low — Grok toasts auto-dismiss quickly — and
        # the recoverable cost is a retry that will re-find the video
        # via /rest/media/post/get.
        banner = await self._scan_for_rate_limit_banner()
        if banner is not None:
            category, banner_text = banner
            from .exceptions import GrokQuotaExceededError, GrokRateLimitError

            if category == "quota_exceeded":
                raise GrokQuotaExceededError(
                    f"Grok reports quota exceeded for this session "
                    f"(visible UI banner: {banner_text!r}). Stop retrying — "
                    f"quota typically resets every 24h. NDJSON-level "
                    f"moderated={gen_result.moderated}; ignore that field, "
                    f"the throttle classification takes precedence."
                )
            raise GrokRateLimitError(
                f"Grok rate-limited this session (visible UI banner: "
                f"{banner_text!r}). Back off 5-10 min and retry, or reduce "
                f"concurrent worker count. NDJSON-level "
                f"moderated={gen_result.moderated}; ignore that field, the "
                f"throttle classification takes precedence."
            )

        # Stash drag metadata on private fields so the wrapper can
        # surface it on VideoExtendResult without another round-trip.
        gen_result.__dict__["_extend_seed_actual"] = actual_seed
        gen_result.__dict__["_extend_seed_displayed"] = displayed_seed
        gen_result.__dict__["_extend_seed_requested"] = seed_start
        return gen_result

    async def extend_video(self, params, **legacy_kwargs) -> VideoExtendResult:
        """Extend a specific video by its post id.

        Canonical shape (v0.19.0+) — dict-style, matching create_video /
        create_image / edit_image::

            await client.extend_video({
                "video_id": "abc-123",
                "prompt": "continue the scene",
                "duration": "6s",
            })

        Thin composition of the public primitives :meth:`select_post`
        and :meth:`extend_current`, plus favorite-state cleanup and
        result enrichment. If you want the composition to be more
        explicit in your own code, you can call those two directly —
        ``extend_video`` is the convenience shape for the common case.

        Flow: ``select_post`` lands the tab on the requested post
        (correcting for Grok's SPA redirect-to-chain-tail behavior),
        then ``extend_current`` opens the extend panel and submits.
        With the correct post selected, the filmstrip's default handle
        position is video_id's own tail, so ``seed_start=None``
        produces fanout-safe branching without any drag. Explicit
        ``seed_start`` still triggers the drag for mid-source seeding.

        Args:
            params: Dict with keys from EXTEND_KEYS (see grok_web.schema).
                ``video_id`` is required; everything else is optional.
                Per-key descriptions below are generated from
                ``grok_web.schema.PARAMS`` (SSOT).

                <SCHEMA_ARGS>

        Returns:
            VideoExtendResult with new video_id, source linkage,
            moderation verdict, seed drift metadata, and
            duration_s / cumulative_duration_s.

        Raises:
            GrokAPIError: If select_post cannot land on video_id,
                the menu item is missing, the seed drag drifts
                outside ``SEED_DRIFT_TOLERANCE`` (1.0s), or
                generation fails.
            TypeError: If ``params`` is not a dict (or, for the
                deprecated positional form, a ``video_id`` string), or
                if ``video_id`` is missing.

        Legacy form (deprecated v0.19.0, removed v0.20.0)::

            await client.extend_video("abc-123", prompt="...", duration="6s")

        Still works but emits ``DeprecationWarning``.
        """
        import warnings

        from .schema import EXTEND_KEYS, validate_params

        if isinstance(params, str):
            warnings.warn(
                "extend_video(video_id, ...) positional/kwarg form is "
                "deprecated since v0.19.0; use extend_video({'video_id': ..., "
                "...}) for consistency with create_video / edit_image. "
                "Removed in v0.20.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            params = {"video_id": params, **legacy_kwargs}
        elif isinstance(params, dict):
            if legacy_kwargs:
                raise TypeError(
                    "extend_video: cannot mix dict params and kwargs. "
                    "Pass everything inside the dict."
                )
        else:
            raise TypeError(
                f"extend_video: first arg must be a dict (canonical) or "
                f"a video_id str (deprecated), got {type(params).__name__}"
            )

        p = validate_params(params, EXTEND_KEYS)
        video_id = p.get("video_id")
        if not video_id:
            raise TypeError("extend_video: 'video_id' is required in params dict")
        seed_start = p.get("seed_start")
        duration = p.get("duration")
        prompt = p.get("prompt")
        # extend_video defaults to 600s (not the shared schema default of 300)
        # for the same reason as create_video — extend under NSFW or queue
        # pressure regularly needs >300s. Read from the original `params`
        # dict so an explicit caller value still wins.
        timeout = params.get("timeout", 600)
        preserve_source_favorite_state = p.get("preserve_source_favorite_state", False)

        # One get_post_details call does double duty: the duration
        # hint (skips a wait_for_filmstrip bootstrap) and the
        # favorite-state snapshot if the caller opted in.
        try:
            details = await self.get_post_details(video_id)
        except Exception as e:
            raise GrokAPIError(
                f"extend_video: could not fetch post details for {video_id}: {e}"
            ) from e
        raw = details.raw_data or {}
        post = raw.get("post", raw)
        own = post.get("videoDuration")
        start = post.get("videoExtensionStartTime") or 0
        video_duration_hint: float | None = None
        if own is not None:
            video_duration_hint = float(start) + float(own)

        # Known Grok behavior: clicking 扩展 silently appends the
        # source video to favorites on every call. Only auto-revert
        # when the caller opts in AND we can confirm the source was
        # NOT favorited pre-call.
        was_source_favorited: bool | None = None
        if preserve_source_favorite_state:
            was_source_favorited = await self._get_favorited_state(video_id)
        elif not self._favorite_pollution_hinted:
            logger.info(
                "[extend_video] Grok auto-favorites the source video on "
                "each UI-driven extend call, which accumulates duplicate "
                "entries in the user's favorites tab. Pass "
                "preserve_source_favorite_state=True to have the connector "
                "snapshot-and-revert the state (only when the source was "
                "unambiguously not favorited pre-call — safe default). "
                "Hint fires once per client."
            )
            self._favorite_pollution_hinted = True

        # Correctness: select_post guarantees video_id is actually
        # shown even when Grok's SPA redirects to a descendant.
        await self.select_post({"post_id": video_id})

        gen_result = await self.extend_current(
            seed_start=seed_start,
            duration=duration,
            prompt=prompt,
            timeout=timeout,
            video_duration_hint=video_duration_hint,
        )

        # Duration enrichment from post metadata (skip moderated).
        duration_s: int | None = None
        cumulative_duration_s: float | None = None
        if not gen_result.moderated and gen_result.video_id:
            duration_s, cumulative_duration_s = await self._fetch_video_duration(
                gen_result.video_id
            )

        # Phantom detection: Grok occasionally returns a structurally
        # valid streamingVideoGenerationResponse (with a UUID-formatted
        # videoId and moderated=False) even though no real extension
        # occurred — the "new" post just echoes the source's metadata
        # and eventually becomes unfetchable. Observed on specific
        # prompt+source combinations where the pipeline silently drops
        # the generation upstream of the usual moderation verdict.
        #
        # Signal: a real extension always grows chain-coord duration
        # past the source's own tail (which we already computed as
        # video_duration_hint). If the new post reports
        # cumulative_duration_s <= source_tail, the response was fake.
        # Restore favorite state BEFORE we raise so retry loops don't
        # leave the user's account in a weird state.
        if preserve_source_favorite_state and was_source_favorited is False:
            try:
                await self.unfavorite_post(video_id)
            except Exception as _e:
                logger.warning(
                    f"[extend_video] could not restore favorite state for {video_id}: {_e}"
                )
        if (
            not gen_result.moderated
            and gen_result.video_id
            and video_duration_hint is not None
            and cumulative_duration_s is not None
            and cumulative_duration_s <= video_duration_hint + 0.01  # float slack
        ):
            from .exceptions import GrokGenerationFailedError

            raise GrokGenerationFailedError(
                f"extend_video: phantom response — Grok returned video_id "
                f"{gen_result.video_id!r} with moderated=False but "
                f"cumulative_duration_s ({cumulative_duration_s:.2f}s) did "
                f"not grow past the source's own tail "
                f"({video_duration_hint:.2f}s). No real extension occurred; "
                "Grok silently dropped the generation upstream of the "
                "usual moderation verdict. The returned video_id will "
                "typically become unfetchable within seconds. Retrying "
                "with the same prompt may reproduce; vary the prompt "
                "phrasing or seed_start to recover."
            )

        # Recover seed drag metadata stashed by extend_current.
        seed_actual = gen_result.__dict__.get("_extend_seed_actual")
        seed_displayed = gen_result.__dict__.get("_extend_seed_displayed")
        seed_requested = gen_result.__dict__.get("_extend_seed_requested")

        # is_persisted probe — see VideoGenerationResult docstring for
        # rationale. ~150ms; best-effort.
        is_persisted: bool | None = None
        if gen_result.video_id:
            try:
                await self.get_post_details(gen_result.video_id)
                is_persisted = True
            except GrokNotFoundError:
                is_persisted = False
            except Exception:
                pass

        return VideoExtendResult(
            video_id=gen_result.video_id,
            source_video_id=video_id,
            parent_post_id=gen_result.parent_post_id or video_id,
            moderated=gen_result.moderated,
            progress=gen_result.progress,
            mode=gen_result.mode,
            model_name=gen_result.model_name,
            conversation_id=gen_result.conversation_id,
            statsig_id=gen_result.statsig_id,
            seed_start_requested=seed_requested,
            seed_start_actual=seed_actual,
            seed_start_displayed=seed_displayed,
            duration_s=duration_s,
            cumulative_duration_s=cumulative_duration_s,
            is_persisted=is_persisted,
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
        """Download a video file by URL using the browser's fetch API.

        Grok's video CDN has two slightly different auth modes depending
        on which storage tier the asset lives in:

        1. ``imagine-public.x.ai/.../share-videos/...`` — accepts
           ``?dl=1`` + ``credentials: 'omit'``. This is the path used
           by most create_video / extend_video outputs.
        2. ``assets.grok.com/...`` (signed URL) — rejects
           ``?dl=1`` with HTTP 403; needs ``credentials: 'include'``.
           Happens when Grok routes certain extend outputs through
           authenticated storage.

        Try (1) first (common case, fastest), fall back to (2) on
        4xx/5xx. Each attempt is a page-context fetch that returns
        headers + body or an error code.
        """
        import asyncio
        import base64
        import json as json_module

        # Ensure we're on grok.com (required for proper cookie context)
        current_url = await self._tab.evaluate("window.location.href", await_promise=False)
        if not current_url or "grok.com" not in str(current_url):
            await self._tab.get(f"{self.BASE_URL}/imagine")
            await asyncio.sleep(3)

        async def _fetch(fetch_url: str, credentials: str) -> dict:
            js_code = f"""
            (async () => {{
                try {{
                    const response = await fetch({json_module.dumps(fetch_url)}, {{
                        credentials: '{credentials}',
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
                    return JSON.stringify({{"status": 200, "data": base64}});
                }} catch (e) {{
                    return JSON.stringify({{"status": 0, "error": e.message}});
                }}
            }})()
            """
            raw = await self._tab.evaluate(js_code, await_promise=True, return_by_value=True)
            return json_module.loads(raw)

        # Build attempt list: (url_to_fetch, credentials_mode, description)
        sep = "&" if "?" in video_url else "?"
        attempts = [
            (f"{video_url}{sep}dl=1", "omit", "dl=1 + omit (standard)"),
            (video_url, "include", "no-dl + include-creds (signed URLs)"),
            (video_url, "omit", "no-dl + omit (permissive CDN)"),
        ]

        last_error = None
        for fetch_url, creds, desc in attempts:
            result = await _fetch(fetch_url, creds)
            if result.get("status") == 200:
                video_data = base64.b64decode(result["data"])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                # Explicit flush + fsync + post-close size check. On
                # Windows, Path.write_bytes returns before the OS finishes
                # making the file readable to other processes — a caller
                # that subprocesses ffprobe immediately can hit empty/
                # partial state ~20-30% of the time. fsync forces the
                # writeback before we return; the size check catches the
                # rare disk-full / truncated-write case.
                import os as _os

                with open(output_path, "wb") as _f:
                    _f.write(video_data)
                    _f.flush()
                    _os.fsync(_f.fileno())
                actual = output_path.stat().st_size
                if actual != len(video_data):
                    raise GrokAPIError(
                        f"download_video: size mismatch after write — "
                        f"expected {len(video_data)} bytes, got {actual} "
                        f"(URL: {video_url[:80]}). Disk full or filesystem "
                        "race; retry the call."
                    )
                logger.debug(
                    "[download_video] success via %s for %s",
                    desc,
                    video_url[:80],
                )
                return output_path
            last_error = result.get("error", "Unknown error")
            logger.debug(
                "[download_video] %s failed: %s — trying next strategy",
                desc,
                last_error,
            )

        raise GrokAPIError(
            f"Download failed after all fallback strategies. "
            f"URL: {video_url[:100]}, Last error: {last_error}"
        )

    async def edit_image(self, params: dict) -> ImageEditResult:
        """Edit an image to generate new variations, with optional refs.

        Canonical shape (v0.18.0+) — semi-structured ``prompt`` + ``images``::

            await client.edit_image({
                "prompt": "edit @1 to add ropes from @2 colored like @3",
                "images": ["post:source", "post:ref_a", "post:ref_b"],
            })

        ``images[0]`` is the post being edited; ``images[1:]`` are
        reference images. ``@1`` in the prompt = source, ``@2..@N+1`` =
        refs. Each entry may be ``"post:<uuid>"`` (Grok auto-downloads
        the media into a temp file and re-uploads) or a local file
        path.

        Args:
            params: Dict with keys from EDIT_KEYS (see grok_web.schema).
                Per-key descriptions below are generated from
                ``grok_web.schema.PARAMS`` (SSOT).

                <SCHEMA_ARGS>

        Returns:
            ImageEditResult with image URLs and moderation info.

        Raises:
            GrokAPIError(WKE=imagine:invalid-parent-post): The source post
                ``images[0]`` is rejected by Grok's server-side lineage
                validator. As of 2026-04 this fires for **chain-root
                posts** — image posts that have video descendants. Such
                posts can be viewed normally but Grok refuses them as an
                edit_image source. Workarounds: (a) use a non-chain-root
                image post as source, or (b) pass the chain-root post as
                a *reference* image (``images=[<other_source>,
                'post:<chain_root>']``) rather than as the source.

        Examples:
            # Single-source (no refs) — direct REST when sid available
            await client.edit_image({
                "prompt": "add wings",
                "images": ["post:abc-123"],
            })

            # Multi-ref — uses UI path (REST single-ref-only today)
            await client.edit_image({
                "prompt": "@1 character with @2 outfit and @3 lighting",
                "images": ["post:src", "post:outfit_ref", "post:light_ref"],
            })

            # Legacy shape (deprecated, still works with a DeprecationWarning):
            await client.edit_image({
                "post_id": "abc-123",
                "edit_prompt": "add wings",
            })
        """
        import warnings

        from .prompt_parser import classify_image_source
        from .schema import EDIT_KEYS, validate_params

        p = validate_params(params, EDIT_KEYS)

        # Canonical input is `prompt` + `images`. Legacy is `post_id` +
        # `edit_prompt`; normalize and warn. Either form works through
        # v0.18.x; legacy form will be removed in v0.19.0.
        canonical_prompt = p.get("prompt")
        canonical_images = p.get("images")
        legacy_post_id = p.get("post_id")
        legacy_edit_prompt = p.get("edit_prompt")

        if canonical_images is None and legacy_post_id:
            warnings.warn(
                "edit_image: 'post_id' is deprecated since v0.18.0; "
                "use images=[f'post:{post_id}', ...] instead. "
                "post_id will be removed in v0.19.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            canonical_images = [f"post:{legacy_post_id}"]
        if canonical_prompt is None and legacy_edit_prompt is not None:
            warnings.warn(
                "edit_image: 'edit_prompt' is deprecated since v0.18.0; "
                "use 'prompt' instead. edit_prompt will be removed in v0.19.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            canonical_prompt = legacy_edit_prompt

        if not canonical_images:
            raise GrokAPIError(
                "edit_image: 'images' is required (or legacy 'post_id'). "
                "First entry is the post being edited; rest are references."
            )
        if canonical_prompt is None:
            raise GrokAPIError("edit_image: 'prompt' is required (or legacy 'edit_prompt').")

        source_kind, source_value = classify_image_source(canonical_images[0])
        if source_kind != "post":
            raise GrokAPIError(
                f"edit_image: images[0] (the source being edited) must be "
                f"'post:<uuid>'; got {canonical_images[0]!r}"
            )

        # Re-bind to the existing local names so the rest of the body
        # (REST primary + UI fallback) doesn't need rewriting.
        post_id = source_value
        edit_prompt = canonical_prompt
        ref_specs: list[str] = list(canonical_images[1:])

        timeout = p.get("timeout", 60)

        # Multi-ref always takes the UI path. The direct REST primary
        # was built for single-ref edits; multi-ref via REST would need
        # an extra probe of Grok's image_edit payload shape that we
        # haven't done. UI is naturally paced and verified working.
        if ref_specs:
            import tempfile

            tmpdir = Path(tempfile.mkdtemp(prefix="grok_edit_refs_"))
            try:
                ref_paths = await self._resolve_image_refs_to_local(ref_specs, tmpdir)
                await self.select_post({"post_id": post_id})
                return await self.edit_current(
                    edit_prompt,
                    timeout=timeout,
                    source_post_id=post_id,
                    reference_images=ref_paths,
                )
            finally:
                for f in tmpdir.iterdir():
                    f.unlink(missing_ok=True)
                tmpdir.rmdir()
        import asyncio
        import json as json_mod

        from ai_dev_browser import cdp

        d = self._ui_delay

        # 2026-04 edit_image dispatch:
        #
        # Grok's post pages redirect /imagine/post/<image_id> to the
        # tail video when the image has video descendants, which
        # collapses the UI driving path on any non-trivial chain — the
        # sidebar no longer shows the root image thumbnail, and the "…"
        # menu's image-context items (Custom / Spicy / Normal / 删除帖子)
        # are replaced by video-context items (扩展 / 删除视频).
        #
        # Primary path: POST /rest/app-chat/conversations/new directly
        # via page-context fetch, using the payload shape Grok's own UI
        # sends. Works on any chain depth since it doesn't touch UI
        # state. Requires a cached x-statsig-id — populated by the
        # snitch whenever the session has previously fired
        # /rest/app-chat/conversations/new (any create_video /
        # extend_video / edit_image call).
        #
        # Fallback path: UI driving (kept for sessions where the sid
        # cache is cold — e.g. edit_image is the very first generation
        # call of the session). Works for shallow chains; raises a
        # clear error on deep chains telling the caller to run any
        # other generation call first to warm the sid cache.

        statsig_id: str | None = None
        if self._statsig_snitch is not None:
            statsig_id = await self._statsig_snitch.get(
                "/rest/app-chat/conversations/new", timeout=0.1
            )

        if not statsig_id:
            # No sid yet — fall back to UI path.
            return await self._edit_image_via_ui(post_id, edit_prompt, timeout)

        # Ensure we're on grok.com (for cookie context on the fetch).
        current_url_raw = await self._tab.evaluate("window.location.href", await_promise=False)
        if not current_url_raw or "grok.com" not in str(current_url_raw):
            await self._tab.get(f"{self.BASE_URL}/imagine")
            await asyncio.sleep(3 * d)

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
                                    # Update image data (later responses have final status).
                                    # `post_id` is an alias for image_id — each edit output
                                    # IS a Grok post and the UUID is the same. Exposed as
                                    # a distinct key so callers feeding results into
                                    # create_video({"images": ["post:<id>"]}) don't have
                                    # to remember that image_id == post_id.
                                    captured_data["images"][image_id] = {
                                        "image_id": image_id,
                                        "post_id": image_id,
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

        # Resolve the source image's public URL. The image_edit endpoint
        # wants a resolvable https URL in imageReferences; using the
        # mediaUrl from post metadata handles user-uploaded images
        # (which live at a different CDN path than Grok-generated ones)
        # as well as the share-images path we'd otherwise construct.
        #
        # On GrokNotFoundError, we used to fall straight to UI (v0.19.5).
        # That made the chain-root edit case dependent on the UI menu
        # working — which it doesn't (the chain-root layout has both
        # image-card and video-card menus and Grok's menu items reflect
        # global viewport state, not which trigger we click). v0.19.10
        # tries the constructed-URL pattern on 404 and stays in the REST
        # path; if Grok rejects the request later (4xx), the existing
        # 4xx-fallback-to-UI handler still kicks in.
        constructed_media_url = f"https://imagine-public.x.ai/imagine-public/images/{post_id}.jpg"
        try:
            src_details = await self.get_post_details(post_id)
        except GrokNotFoundError:
            logger.info(
                "edit_image: get_post_details(%s) returned 404 — post "
                "exists at the UI layer but REST media/post/get can't "
                "resolve it (common for edit_image-derived chain-root "
                "posts in 2026-04). Trying the constructed-URL pattern "
                "(%s) with REST; will fall through to UI on 4xx.",
                post_id,
                constructed_media_url,
            )
            src_details = None
        except Exception as e:
            raise GrokAPIError(
                f"edit_image: could not fetch post details for {post_id}: {e}"
            ) from e
        src_media_url = (src_details.media_url if src_details else None) or constructed_media_url

        # statsig_id already resolved at top of edit_image — reuse.
        # Build the exact request body Grok's own UI sends. Captured
        # 2026-04-22 by observing a successful edit_image UI run.
        request_body = {
            "temporary": True,
            "modelName": "imagine-image-edit",
            "message": edit_prompt,
            "enableImageGeneration": True,
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "enableImageStreaming": True,
            "imageGenerationCount": 2,
            "forceConcise": False,
            "toolOverrides": {"imageGen": True},
            "enableSideBySide": True,
            "sendFinalMetadata": True,
            "isReasoning": False,
            "disableTextFollowUps": True,
            "responseMetadata": {
                "modelConfigOverride": {
                    "modelMap": {
                        "imageEditModelConfig": {
                            "imageReferences": [src_media_url],
                            "parentPostId": post_id,
                        },
                        "imageEditModel": "imagine",
                    }
                }
            },
            "disableMemory": False,
            "forceSideBySide": False,
        }

        # POST via page-context fetch. This runs in the grok.com origin,
        # so session cookies flow automatically. We include x-statsig-id
        # manually — Grok's own JS adds it server-side but a page-fetch
        # from our injected JS does not.
        fetch_js = r"""
            (async (body, sid) => {
                try {
                    const headers = {'Content-Type': 'application/json'};
                    if (sid) headers['x-statsig-id'] = sid;
                    const r = await fetch('/rest/app-chat/conversations/new', {
                        method: 'POST',
                        headers: headers,
                        body: body,
                        credentials: 'include',
                    });
                    // Always read body text — the server returns the NDJSON
                    // stream for successes but an error object for 4xx.
                    const text = await r.text();
                    return JSON.stringify({
                        status: r.status,
                        ok: r.ok,
                        body: text.slice(0, 500),
                    });
                } catch (e) {
                    return JSON.stringify({
                        ok: false,
                        error: (e && e.name) ? e.name + ': ' + e.message : String(e),
                    });
                }
            })(__BODY__, __SID__)
        """.replace("__BODY__", json_mod.dumps(json_mod.dumps(request_body))).replace(
            "__SID__", json_mod.dumps(statsig_id) if statsig_id else "null"
        )
        post_result_raw = await self._tab.evaluate(fetch_js, await_promise=True)
        try:
            post_result = (
                json_mod.loads(post_result_raw) if isinstance(post_result_raw, str) else {}
            )
        except Exception:
            post_result = {}
        if not post_result.get("ok"):
            status = post_result.get("status")
            err = post_result.get("error", "unknown")
            body_preview = post_result.get("body", "")
            # Any 4xx from /rest/app-chat/conversations/new means Grok's
            # REST validator rejected the request — observed reasons
            # include anti-bot rules (rapid successive calls), lineage
            # checks on certain post classes (chain-root edit_image
            # outputs), or statsig drift between cache and server. All
            # of these are recoverable via the UI path which Grok treats
            # as higher-trust (DOM-paced, full session context). Drop
            # the cached sid so the next call probes fresh, then fall
            # through. 5xx is genuine server-side and not UI-recoverable
            # so it still raises.
            if isinstance(status, int) and 400 <= status < 500:
                # Fail-fast: Grok's server-side lineage validator rejects
                # certain post classes as edit_image source. The signal
                # is the WKE (well-known error) code in the response body.
                # As of 2026-04, the relevant one is
                # ``imagine:invalid-parent-post`` — emitted for chain-root
                # posts (image posts with video descendants). Falling
                # back to UI doesn't help (UI hits the same validator
                # via /conversations/new), so save the round-trip and
                # tell the caller exactly what happened + the workaround.
                if "invalid-parent-post" in body_preview.lower():
                    raise GrokAPIError(
                        f"edit_image: post {post_id} cannot be used as an "
                        f"edit_image source — Grok server-side validator "
                        f"rejected it (WKE=imagine:invalid-parent-post). "
                        f"This typically means the post is a chain root "
                        f"(an image post that has video descendants) or "
                        f"has lineage state that disqualifies it as an "
                        f"edit source. The UI path also fails on these "
                        f"posts, so the connector skips that fallback. "
                        f"Workarounds: (a) use a non-chain-root image "
                        f"post as the source; (b) pass this post as a "
                        f"reference image instead of as the source "
                        f"(``images=[<other-source>, 'post:{post_id}']``). "
                        f"Original error body: {body_preview!r}"
                    )

                reason = (
                    "anti-bot"
                    if "anti-bot" in body_preview.lower()
                    else f"validator-rejected ({status})"
                )
                logger.info(
                    "edit_image REST path got HTTP %d (%s) — falling back to UI path. body=%r",
                    status,
                    reason,
                    body_preview[:200],
                )
                if self._statsig_snitch is not None:
                    self._statsig_snitch._by_endpoint.pop("/rest/app-chat/conversations/new", None)
                # Detach the current network handlers so the UI fallback's
                # own handlers don't double-fire on the same events.
                try:
                    self._tab.remove_handler(cdp.network.ResponseReceived, handle_response)
                    self._tab.remove_handler(cdp.network.LoadingFinished, handle_loading_finished)
                except Exception:
                    pass
                return await self._edit_image_via_ui(post_id, edit_prompt, timeout)
            raise GrokAPIError(
                f"edit_image REST POST failed: status={status}, "
                f"error={err}, body={body_preview!r}, "
                f"statsig_id={'present' if statsig_id else 'MISSING'}"
            )

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

    async def edit_current(
        self,
        edit_prompt: str,
        *,
        timeout: int = 300,
        source_post_id: str | None = None,
        reference_images: list[Path] | None = None,
    ) -> ImageEditResult:
        """Edit the image post currently selected on the tab.

        Lower-level primitive: opens 更多选项 → Custom → 图片 mode,
        optionally uploads additional reference images, fills the
        prompt, clicks 编辑, and waits for the NDJSON response. Does
        NOT navigate. Caller is responsible for selecting the source
        image first via :meth:`select_post`.

        Reference images: the source post (the one being edited) is
        always available as ``@1`` in the prompt — it's the implicit
        "Image 1" in Grok's @ popup with zero uploads needed. Each
        path in ``reference_images`` is uploaded into the panel and
        becomes ``@2, @3, ...`` in order. So a 2-ref edit_current
        with ``edit_prompt = "edit @1 to add ropes from @2 colored
        like @3"`` and ``reference_images=[ref_a, ref_b]`` resolves
        as: @1 = source (current selection), @2 = ref_a, @3 = ref_b.

        ``source_post_id`` is used only for result metadata
        (``ImageEditResult.post_id``).

        Args:
            edit_prompt: The edit instruction text. May contain
                ``@N`` markers (1 = source, 2..N = reference_images).
            timeout: Max seconds to wait for both candidate images
                to reach progress=100 (default 300).
            source_post_id: Optional label for the result. Does not
                influence the UI interaction.
            reference_images: Optional local file paths to upload as
                additional refs. Up to ~6 (Grok's UI cap). Use
                :meth:`edit_image` if you want the connector to
                resolve ``post:<uuid>`` / file paths for you.

        Returns:
            ImageEditResult. The ``post_id`` field reflects
            ``source_post_id`` (or empty if omitted).
        """
        import asyncio
        import json as json_mod

        from ai_dev_browser import cdp

        from .actions.extend_seed import enable_focus_emulation

        d = self._ui_delay

        await self._tab.send(cdp.network.enable())

        captured_data: dict[str, Any] = {"conversation_id": None, "images": {}}

        async def handle_response(event: cdp.network.ResponseReceived):
            url = event.response.url
            if "/app-chat/conversations/new" in url:
                captured_data["request_id"] = event.request_id

        async def handle_loading_finished(event: cdp.network.LoadingFinished):
            if captured_data.get("request_id") == event.request_id:
                try:
                    body_result = await self._tab.send(
                        cdp.network.get_response_body(request_id=event.request_id)
                    )
                    body = body_result[0] if isinstance(body_result, tuple) else str(body_result)
                    for line in body.strip().split("\n"):
                        if not line:
                            continue
                        try:
                            data = json_mod.loads(line)
                            result = data.get("result", {})
                            if "conversation" in result:
                                captured_data["conversation_id"] = result["conversation"].get(
                                    "conversationId"
                                )
                            response = result.get("response", {})
                            if "streamingImageGenerationResponse" in response:
                                img_resp = response["streamingImageGenerationResponse"]
                                image_id = img_resp.get("imageId")
                                if image_id:
                                    captured_data["images"][image_id] = {
                                        "image_id": image_id,
                                        "post_id": image_id,
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

        await asyncio.sleep(1 * d)
        await enable_focus_emulation(self._tab)

        # 0. Switch viewport to image-view mode. select_post locks the
        # URL onto the source image, but for chain-root images that have
        # video descendants Grok's post page defaults the viewport to
        # video-mode (showing the tail video player). The "..." menu
        # tracks the visible viewport, so a video-mode viewport gives
        # video-context items (扩展 / 删除视频) instead of the image
        # items we need (Custom / Spicy / Normal). switch_to_image_view
        # is a no-op when the post has no video children or the
        # viewport is already image-mode, so it's safe to call
        # unconditionally.
        from .actions.post_media import switch_to_image_view
        from .actions.post_menu import click_menu_item, open_post_menu

        await switch_to_image_view(self._tab, delay=d)

        # 1. Open 更多选项 / More options / Options — use the resilient
        # post_menu helper rather than handwritten JS so a Grok-side
        # aria-label rename only needs to be patched in one place
        # (actions/post_menu.py::_MENU_BUTTON_NAMES). The helper retries
        # 3x, multi-locale, and verifies the menu actually opened by
        # checking that role=menuitem nodes appeared.
        #
        # prefer_media="image": chain-root posts render BOTH image and
        # video cards (each with its own '...' trigger). We want the
        # image-context menu (Custom / Spicy / Normal items), not the
        # video-context one (扩展 / 删除视频). The helper picks the
        # trigger spatially closest to the largest visible <img>.
        await open_post_menu(self._tab, delay=d, prefer_media="image")
        await asyncio.sleep(0.2 * d)

        # 3. Click Custom (or legacy 编辑图像 / Edit image). Don't
        # wrap click_menu_item's error — it already includes the
        # actual menu items it found, which is the diagnostic info
        # we need. (Earlier wrapper used to swallow that and add a
        # speculative "viewport may still be in video-mode" hint;
        # turned out chain-root failures are server-side lineage
        # rejections, not viewport mode, so the inner dump is more
        # actionable.)
        await click_menu_item(self._tab, "Custom", "编辑图像", "Edit image", delay=d)
        await asyncio.sleep(2.5 * d)

        # 4. Switch to 图片 mode (panel defaults to 视频)
        await self._tab.evaluate(
            r"""
            (() => {
                const b = Array.from(document.querySelectorAll('button'))
                    .find(x => (x.getAttribute('aria-label')||'')==='图片');
                if (!b) return;
                const r = b.getBoundingClientRect();
                const x = r.x + r.width/2, y = r.y + r.height/2;
                const o = {bubbles:true, cancelable:true, clientX:x, clientY:y,
                           pointerType:'mouse', button:0, pointerId:1, isPrimary:true};
                b.dispatchEvent(new PointerEvent('pointerdown', o));
                b.dispatchEvent(new MouseEvent('mousedown', o));
                b.dispatchEvent(new PointerEvent('pointerup', o));
                b.dispatchEvent(new MouseEvent('mouseup', o));
                b.dispatchEvent(new MouseEvent('click', o));
            })()
            """
        )
        await asyncio.sleep(1.0 * d)

        # 4b. Upload reference images (if any). One setFileInputFiles call
        # for ALL refs — calling it multiple times REPLACES rather than
        # appends. Probe-verified: the source post is the implicit @1 in
        # Grok's @ popup; each upload becomes @2, @3, ... in order.
        if reference_images:
            from ai_dev_browser import cdp as _cdp_refs

            from .actions.imagine_input import _count_uploaded_images, set_prompt_with_refs
            from .prompt_parser import parse_prompt

            doc = await self._tab.send(_cdp_refs.dom.get_document(-1, True))
            node_id = await self._tab.send(
                _cdp_refs.dom.query_selector(doc.node_id, 'input[type="file"][name="files"]')
            )
            if not node_id:
                raise GrokAPIError(
                    "edit_current: file input not found in the edit panel — "
                    "Grok UI may have changed."
                )
            from ai_dev_browser.core._element import filter_recurse

            node = filter_recurse(doc, lambda n: n.node_id == node_id)
            await self._tab.send(
                _cdp_refs.dom.set_file_input_files(
                    [str(p.absolute()) for p in reference_images],
                    backend_node_id=node.backend_node_id,
                )
            )
            # Wait for the Remove buttons to appear, proving uploads landed.
            deadline = asyncio.get_event_loop().time() + 15
            while asyncio.get_event_loop().time() < deadline:
                cnt = await _count_uploaded_images(self._tab)
                if cnt >= len(reference_images):
                    break
                await asyncio.sleep(0.5)
            else:
                raise GrokAPIError(
                    f"edit_current: only {cnt}/{len(reference_images)} reference "
                    "images appeared after upload (timed out at 15s)."
                )

            # Validate @N markers against combined image list (source + refs).
            # parse_prompt raises if @N out of range — surface that clearly.
            full_image_list = ["__source__", *[str(p) for p in reference_images]]
            try:
                segments = parse_prompt(edit_prompt, full_image_list)
            except ValueError as e:
                raise GrokAPIError(
                    f"edit_current: {e} (source counts as @1, "
                    f"reference_images map to @2..@{len(full_image_list)})"
                ) from e
        else:
            segments = []

        # 5. Fill prompt. If we have refs AND the prompt uses @N markers,
        # walk segments via set_prompt_with_refs (types text + types @ +
        # clicks "Image N" per ref). Otherwise plain execCommand insert.
        prompt_filled_via_refs = False
        if reference_images and segments and any(s["type"] == "ref" for s in segments):
            from .actions.imagine_input import set_prompt_with_refs

            await set_prompt_with_refs(self._tab, segments, delay=self._ui_delay)
            prompt_filled_via_refs = True
            await asyncio.sleep(1 * d)

        if not prompt_filled_via_refs:
            escaped_prompt = (
                edit_prompt.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
            )
            fill_result = await self._tab.evaluate(
                f"""
                (() => {{
                    const ed = document.querySelector('.tiptap.ProseMirror')
                           || document.querySelector('[contenteditable="true"]');
                    if (!ed) return 'no-editor';
                    ed.focus();
                    document.execCommand('selectAll');
                    document.execCommand('delete');
                    document.execCommand('insertText', false, `{escaped_prompt}`);
                    return 'ok';
                }})()
                """
            )
            if fill_result == "no-editor":
                raise GrokAPIError("Could not find prompt editor (ProseMirror)")
            await asyncio.sleep(1 * d)

        # 6. Click 编辑 submit (fall back to 生成视频 if mode flip failed)
        submit_clicked = await self._tab.evaluate(
            r"""
            (() => {
                let b = Array.from(document.querySelectorAll('button'))
                    .find(x => (x.getAttribute('aria-label')||'')==='编辑');
                if (!b) {
                    b = Array.from(document.querySelectorAll('button'))
                        .find(x => (x.getAttribute('aria-label')||'')==='生成视频');
                }
                if (!b) return false;
                const r = b.getBoundingClientRect();
                const x = r.x + r.width/2, y = r.y + r.height/2;
                const o = {bubbles:true, cancelable:true, clientX:x, clientY:y,
                           pointerType:'mouse', button:0, pointerId:1, isPrimary:true};
                b.dispatchEvent(new PointerEvent('pointerdown', o));
                b.dispatchEvent(new MouseEvent('mousedown', o));
                b.dispatchEvent(new PointerEvent('pointerup', o));
                b.dispatchEvent(new MouseEvent('mouseup', o));
                b.dispatchEvent(new MouseEvent('click', o));
                return true;
            })()
            """
        )
        if not submit_clicked:
            raise GrokAPIError("Could not find 编辑 submit button")

        # Wait for response with timeout
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            completed = [
                img for img in captured_data["images"].values() if img.get("progress") == 100
            ]
            if len(completed) >= 2:
                break
            await asyncio.sleep(1)

        images = list(captured_data["images"].values())
        return ImageEditResult(
            post_id=source_post_id or "",
            edit_prompt=edit_prompt,
            images=images,
            conversation_id=captured_data.get("conversation_id"),
        )

    async def _edit_image_via_ui(
        self, post_id: str, edit_prompt: str, timeout: int
    ) -> ImageEditResult:
        """UI fallback for edit_image when no x-statsig-id is cached.

        Thin composition of :meth:`select_post` and :meth:`edit_current`.
        Works on any chain depth since select_post defensively lands
        the tab on post_id even when Grok's SPA redirects to a
        descendant. See :meth:`edit_image` for the public entry point.
        """
        await self.select_post({"post_id": post_id})
        return await self.edit_current(edit_prompt, timeout=timeout, source_post_id=post_id)

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
            params: Dict with keys from UPLOAD_KEYS (see grok_web.schema).
                Per-key descriptions below are generated from
                ``grok_web.schema.PARAMS`` (SSOT).

                <SCHEMA_ARGS>

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
                    source_post_id=raw.get("originalPostId") or video_id,
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
            params: Dict with keys from IMAGE_KEYS (see grok_web.schema).
                Per-key descriptions below are generated from
                ``grok_web.schema.PARAMS`` (SSOT).

                <SCHEMA_ARGS>

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
        ref_specs: list[str] = list(p.get("images") or [])
        aspect_ratio = p.get("aspect_ratio", "2:3")
        min_success = p.get("min_success", 1)
        max_scroll = p.get("max_scroll", 5)
        timeout = p.get("timeout", 300)
        thumbnail_selector = p.get("thumbnail_selector")
        auto_favorite = p.get("auto_favorite", 0)
        # JSON/CLI-friendly shortcut — wrap the int into the equivalent
        # auto_favorite_first_n callable. Skip when caller already passed
        # a thumbnail_selector explicitly (power-user callable wins, per
        # the schema docstring). Default is 0 (opt-in): persistence
        # mutates the user's grok.com account state and should never
        # happen silently — callers who want a durable post_id must ask.
        if thumbnail_selector is None and auto_favorite:
            from .selectors import auto_favorite_first_n as _auto_fav

            thumbnail_selector = _auto_fav(int(auto_favorite))
        elif thumbnail_selector is None and not auto_favorite and not self._persistence_hinted:
            # One-shot nudge per client instance — tell the caller how
            # to persist if they want to, without spamming every call.
            logger.info(
                "[create_image] auto_favorite=0 (default). Generated "
                "images will be ephemeral on the account — URLs work but "
                "there's no post_id for edit_image / img2vid. Pass "
                "auto_favorite=N to also favorite N as persistent posts "
                "(modifies your grok.com favorites list)."
            )
            self._persistence_hinted = True
        quality = p.get("quality", "speed")
        if quality not in {"speed", "quality"}:
            logger.warning(
                f"Unknown quality {quality!r} (known: 'speed', 'quality'); "
                "passing through — Grok UI may reject."
            )
        import asyncio
        import json as json_mod

        from ai_dev_browser import cdp

        d = self._ui_delay

        # Navigate to imagine page (go to blank first to ensure clean state on reused Chrome)
        current_url = await self._tab.evaluate("window.location.href")
        if "grok.com/imagine" in str(current_url):
            # Already on imagine page (reused Chrome) - force full reload
            logger.debug("[create_image] Reused Chrome on imagine page, forcing reload")
            await self._tab.page_reload()
            await asyncio.sleep(2 * d)
        await self._tab.get(f"{self.BASE_URL}/imagine")
        # Grok's Imagine panel lazy-hydrates the ProseMirror editor; a
        # fixed sleep races the mount on slower/churning builds. Poll up
        # to 30s for the editor to appear so we don't hit Bug #1 flake.
        if not await self._wait_for_selector(".tiptap.ProseMirror", timeout=30):
            raise GrokAPIError(
                "Prompt editor (ProseMirror) did not mount within 30s. "
                "Grok's /imagine hydration may be abnormally slow or the "
                "page structure changed."
            )

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

                        # When completed, construct the image URL.
                        # The WS payload doesn't include a direct URL field
                        # (only image_id), so we template it. Empirically
                        # verified 2026-04-21: the CDN serves
                        # .../images/<id>.jpg (image/jpeg) and returns 404
                        # NoSuchKey for .png / .png?cache=1 (what older
                        # versions of this code generated). Keep the path
                        # exact — no query-string suffix.
                        if data.get("current_status") == "completed":
                            image_id = data.get("image_id", job_id)
                            captured_data["jobs"][job_id]["image_url"] = (
                                f"https://imagine-public.x.ai/imagine-public/images/{image_id}.jpg"
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

        # 2026-04 Grok Imagine UI: the old 模型选择 Radix dropdown is
        # gone. Mode + aspect ratio live inline on the prompt panel as
        # text-toggle buttons (图片 / 视频) and an aria-labelled
        # aspect-ratio dropdown. Submit is still aria-label="提交" but
        # only responds to real CDP mouse input, not JS pointer-event
        # dispatch — the panel runs its own onPointerDown handler that
        # checks Chrome's native pointer state.
        from .actions.extend_seed import enable_focus_emulation

        await enable_focus_emulation(self._tab)

        # Step 1: Make sure 图片 text-toggle is active. On fresh /imagine
        # loads this is the default, but if the Chrome was reused from
        # a 视频 generation the toggle may still be on 视频.
        await self._tab.evaluate(
            r"""
            (() => {
                const b = Array.from(document.querySelectorAll('button'))
                    .find(x => (x.innerText||'').trim() === '图片' && !x.getAttribute('aria-label'));
                if (!b) return;
                const r = b.getBoundingClientRect();
                const x = r.x + r.width/2, y = r.y + r.height/2;
                const o = {bubbles:true, cancelable:true, clientX:x, clientY:y,
                           pointerType:'mouse', button:0, pointerId:1, isPrimary:true};
                b.dispatchEvent(new PointerEvent('pointerdown', o));
                b.dispatchEvent(new MouseEvent('mousedown', o));
                b.dispatchEvent(new PointerEvent('pointerup', o));
                b.dispatchEvent(new MouseEvent('mouseup', o));
                b.dispatchEvent(new MouseEvent('click', o));
            })()
            """
        )
        await asyncio.sleep(0.5 * d)

        # Step 1b: Select speed/quality (new in 2026-04). Default is
        # 'speed'; Grok's UI persists the last choice, so we always
        # explicitly click to avoid inheriting a prior 'quality'
        # selection from a previous session in the same Chrome.
        quality_label = (
            "速度" if quality == "speed" else ("质量" if quality == "quality" else quality)
        )
        await self._tab.evaluate(
            r"""
            (() => {
                const want = "__LABEL__";
                const b = Array.from(document.querySelectorAll('button'))
                    .find(x => (x.innerText||'').trim() === want);
                if (!b) return 'not-found';
                const r = b.getBoundingClientRect();
                const x = r.x + r.width/2, y = r.y + r.height/2;
                const o = {bubbles:true, cancelable:true, clientX:x, clientY:y,
                           pointerType:'mouse', button:0, pointerId:1, isPrimary:true};
                b.dispatchEvent(new PointerEvent('pointerdown', o));
                b.dispatchEvent(new MouseEvent('mousedown', o));
                b.dispatchEvent(new PointerEvent('pointerup', o));
                b.dispatchEvent(new MouseEvent('mouseup', o));
                b.dispatchEvent(new MouseEvent('click', o));
                return 'ok';
            })()
            """.replace("__LABEL__", quality_label)
        )
        await asyncio.sleep(0.3 * d)

        # Step 2: Set aspect ratio. The 宽高比 button opens a popup of
        # aspect-ratio options. Each has text like '2:3', '3:2', '1:1',
        # '9:16', '16:9'. Users can also pass 'portrait' / 'landscape'
        # / 'square' — translate to the concrete ratio labels.
        _aspect_aliases = {
            "portrait": "2:3",
            "landscape": "3:2",
            "square": "1:1",
        }
        aspect_label = _aspect_aliases.get(aspect_ratio, aspect_ratio)
        # Step 2: open 宽高比 popup. Multi-locale aria-label list (Grok
        # 2026-05 sometimes ships English-only on certain account locales
        # / experiments).
        open_result = await self._tab.evaluate(
            r"""
            (() => {
                const wanted = ['宽高比', 'Aspect ratio', 'Aspect Ratio', 'Ratio'];
                const b = Array.from(document.querySelectorAll('button'))
                    .find(x => wanted.includes((x.getAttribute('aria-label')||'').trim()));
                if (!b) return 'no-btn';
                const r = b.getBoundingClientRect();
                const x = r.x + r.width/2, y = r.y + r.height/2;
                const o = {bubbles:true, cancelable:true, clientX:x, clientY:y,
                           pointerType:'mouse', button:0, pointerId:1, isPrimary:true};
                b.dispatchEvent(new PointerEvent('pointerdown', o));
                b.dispatchEvent(new MouseEvent('mousedown', o));
                b.dispatchEvent(new PointerEvent('pointerup', o));
                b.dispatchEvent(new MouseEvent('mouseup', o));
                b.dispatchEvent(new MouseEvent('click', o));
                return 'ok';
            })()
            """
        )
        if open_result != "ok":
            # 2026-05 reproduce: Grok renamed/restructured the aspect
            # button on some locales. Silent fallback meant users got
            # Grok's default landscape and didn't know why. Now: dump
            # candidate buttons so the next failure is debuggable, log
            # a clear warning so caller knows aspect_ratio was ignored.
            candidates = await self._tab.evaluate(
                r"""
                (() => {
                    const out = Array.from(document.querySelectorAll('button'))
                        .filter(b => {
                            const r = b.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        })
                        .map(b => ({
                            aria: (b.getAttribute('aria-label')||'').trim(),
                            text: (b.innerText||'').trim().slice(0, 30),
                        }))
                        .filter(d => d.aria || d.text)
                        .slice(0, 25);
                    return JSON.stringify(out);
                })()
                """
            )
            try:
                cand = json.loads(candidates) if isinstance(candidates, str) else None
            except Exception:
                cand = None
            logger.warning(
                "[create_image] aspect_ratio button (宽高比/Aspect ratio) "
                "not found — generation will use Grok's default aspect "
                "(typically landscape). Requested aspect=%r was IGNORED. "
                "Visible button candidates: %r. If the button has been "
                "renamed, file an issue with this list.",
                aspect_label,
                cand,
            )
        else:
            await asyncio.sleep(0.5 * d)
            # Click the matching menuitem.
            # Match on the FIRST LINE of innerText (Grok 2026-05 added an
            # orientation suffix on a newline, so '9:16' became
            # '9:16\nVertical'). The ratio is still on the first line.
            # Falls back to exact-match for older UI variants.
            click_result = await self._tab.evaluate(
                r"""
                (() => {
                    const want = "__LABEL__";
                    const items = Array.from(document.querySelectorAll('[role="menuitem"]'));
                    const firstLine = el => (el.innerText || '')
                        .trim()
                        .split(/[\n\r]/)[0]
                        .trim();
                    const mi = items.find(x => firstLine(x) === want)
                        || items.find(x => (x.innerText||'').trim() === want);
                    if (!mi) {
                        document.body.click();
                        return JSON.stringify({
                            ok: false,
                            available: items.map(it => (it.innerText||'').trim()).slice(0, 12),
                        });
                    }
                    const r = mi.getBoundingClientRect();
                    const x = r.x + r.width/2, y = r.y + r.height/2;
                    const o = {bubbles:true, cancelable:true, clientX:x, clientY:y,
                               pointerType:'mouse', button:0, pointerId:1, isPrimary:true};
                    mi.dispatchEvent(new PointerEvent('pointerdown', o));
                    mi.dispatchEvent(new MouseEvent('mousedown', o));
                    mi.dispatchEvent(new PointerEvent('pointerup', o));
                    mi.dispatchEvent(new MouseEvent('mouseup', o));
                    mi.dispatchEvent(new MouseEvent('click', o));
                    return JSON.stringify({ok: true});
                })()
                """.replace("__LABEL__", aspect_label)
            )
            try:
                cr = json.loads(click_result) if isinstance(click_result, str) else {}
            except Exception:
                cr = {}
            if not cr.get("ok"):
                logger.warning(
                    "[create_image] aspect_ratio menuitem %r not found in "
                    "popup. Available menuitems: %r. Generation will use "
                    "Grok's currently-selected aspect (NOT what was "
                    "requested). Common cause: Grok renamed the labels "
                    "(e.g. '9:16' → '9:16 (portrait)').",
                    aspect_label,
                    cr.get("available"),
                )
            await asyncio.sleep(0.5 * d)

        # Step 2.5: Upload reference images (if any). One setFileInputFiles
        # call for ALL refs — calling it multiple times REPLACES rather
        # than appends. Each upload becomes @1, @2, ... in Grok's @
        # popup (no implicit source on the Imagine homepage, unlike
        # edit_image's edit panel where @1 is the source).
        ref_paths: list[Path] = []
        ref_tmpdir: Path | None = None
        if ref_specs:
            import tempfile

            ref_tmpdir = Path(tempfile.mkdtemp(prefix="grok_create_image_refs_"))
            try:
                ref_paths = await self._resolve_image_refs_to_local(ref_specs, ref_tmpdir)
                doc = await self._tab.send(cdp.dom.get_document(-1, True))
                node_id = await self._tab.send(
                    cdp.dom.query_selector(doc.node_id, 'input[type="file"][name="files"]')
                )
                if not node_id:
                    raise GrokAPIError("create_image: file input not found on Imagine page")
                from ai_dev_browser.core._element import filter_recurse

                node = filter_recurse(doc, lambda n: n.node_id == node_id)
                await self._tab.send(
                    cdp.dom.set_file_input_files(
                        [str(p.absolute()) for p in ref_paths],
                        backend_node_id=node.backend_node_id,
                    )
                )
                # Wait for Remove buttons to appear
                from .actions.imagine_input import _count_uploaded_images

                deadline = asyncio.get_event_loop().time() + 15
                while asyncio.get_event_loop().time() < deadline:
                    cnt = await _count_uploaded_images(self._tab)
                    if cnt >= len(ref_paths):
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise GrokAPIError(
                        f"create_image: only {cnt}/{len(ref_paths)} reference "
                        "images appeared after upload (timed out at 15s)."
                    )
            except BaseException:
                # Best-effort cleanup on any error path
                if ref_tmpdir and ref_tmpdir.exists():
                    for f in ref_tmpdir.iterdir():
                        f.unlink(missing_ok=True)
                    ref_tmpdir.rmdir()
                raise

        # Step 3: Fill the prompt into the tiptap editor.
        # If we have refs AND the prompt uses @N markers, walk segments
        # via set_prompt_with_refs (types text, types @, clicks 'Image N').
        # Otherwise plain execCommand insert (tiptap-safe).
        if not await self._wait_for_selector(".tiptap.ProseMirror", timeout=10):
            raise GrokAPIError("Prompt editor (ProseMirror) disappeared after aspect selection")

        prompt_filled_via_refs = False
        if ref_specs and prompt:
            from .actions.imagine_input import set_prompt_with_refs
            from .prompt_parser import parse_prompt

            try:
                segments = parse_prompt(prompt, [str(p) for p in ref_paths])
            except ValueError as e:
                raise GrokAPIError(
                    f"create_image: {e} (images map to @1..@{len(ref_paths)})"
                ) from e
            if any(s["type"] == "ref" for s in segments):
                await set_prompt_with_refs(self._tab, segments, delay=self._ui_delay)
                prompt_filled_via_refs = True
                await asyncio.sleep(1 * d)

        if not prompt_filled_via_refs:
            escaped_prompt = prompt.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
            fill_result = await self._tab.evaluate(
                f"""
                (() => {{
                    const ed = document.querySelector('.tiptap.ProseMirror');
                    if (!ed) return 'not-found';
                    ed.focus();
                    document.execCommand('selectAll');
                    document.execCommand('delete');
                    document.execCommand('insertText', false, `{escaped_prompt}`);
                    return 'ok';
                }})()
                """
            )
            if fill_result == "not-found":
                raise GrokAPIError("Could not find prompt editor (ProseMirror)")
            await asyncio.sleep(1 * d)

        # Step 4: Click the 提交 submit via real CDP mouse. The button's
        # handler rejects JS-synthesised PointerEvents (checks
        # isTrusted / tracks native pointer state) but responds to
        # Input.dispatchMouseEvent immediately.
        submit_rect = await self._tab.evaluate(
            r"""
            JSON.stringify((() => {
                const b = document.querySelector('button[aria-label="提交"]');
                if (!b) return null;
                const r = b.getBoundingClientRect();
                return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
            })())
            """
        )
        import json as _json

        sr = _json.loads(submit_rect) if isinstance(submit_rect, str) else submit_rect
        if not sr:
            raise GrokAPIError("Could not find 提交 submit button")
        for _ev, _btn, _cc in [
            ("mouseMoved", cdp.input_.MouseButton.NONE, 0),
            ("mousePressed", cdp.input_.MouseButton.LEFT, 1),
            ("mouseReleased", cdp.input_.MouseButton.LEFT, 1),
        ]:
            await self._tab.send(
                cdp.input_.dispatch_mouse_event(
                    type_=_ev,
                    x=float(sr["x"]),
                    y=float(sr["y"]),
                    button=_btn,
                    click_count=_cc,
                    pointer_type="mouse",
                )
            )
            await asyncio.sleep(0.05)

        # Step 5b: Fail-fast if no WS frames arrive shortly after submit.
        #
        # If an overlay (cookie banner, auth modal, etc.) intercepted
        # the submit click, ProseMirror still has our prompt and the
        # page title updates — but no jobs will ever come, and the
        # default timeout (300s) makes a silent bug feel like a hang.
        # 30s is generous for the first ``json`` frame (normally arrives
        # in <3s) and short enough to surface the overlay hint quickly.
        wait_first = 30
        start_first = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_first < wait_first:
            if len(captured_data["jobs"]) > 0:
                break
            await asyncio.sleep(0.5)
        if len(captured_data["jobs"]) == 0:
            raise GrokAPIError(
                f"Submit click fired but no WebSocket generation frames "
                f"received within {wait_first}s. Most likely cause: an "
                "overlay (cookie banner, auth modal, etc.) intercepted "
                "the click via z-index/hit-test. The connector auto-kills "
                "Grok's OneTrust banner on __aenter__, so if you see this "
                "there's a new overlay — inspect the tab for "
                "[role='dialog'] or high-z-index elements covering the "
                "提交 button coords."
            )

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

            # When no new jobs after 3 scroll cycles, two possibilities:
            #   (a) Grok is rate-limiting generation but submit still works
            #       — new batches will eventually appear; backoff and retry.
            #   (b) Grok server-side disabled the submit button (hourly limit,
            #       quota exhausted, preflight content moderation block,
            #       account flag). No new jobs will EVER appear no matter
            #       how long we wait. Used to silently loop until
            #       max_scroll abort, wasting 10-30 min per call.
            #
            # Probe submit-button state + nearby banners to distinguish.
            # The probe is cheap (one JS evaluate) and only fires when
            # we'd otherwise be sleeping anyway.
            if consecutive_no_new_jobs >= 3:
                from .exceptions import GrokQuotaExceededError, GrokRateLimitError

                submit_state = await self._probe_submit_state()
                if submit_state.get("submit_disabled"):
                    banners = submit_state.get("banners") or []
                    candidate_messages = submit_state.get("candidate_messages") or []
                    # Combine both pools so wide-net text catches what the
                    # narrow banner-selector misses. Grok sometimes renders
                    # rate-limit messages in plain <div>s with no
                    # alert/banner/role hint.
                    all_text_pool = list(banners) + [
                        cm.get("text", "") for cm in candidate_messages
                    ]
                    combined_text = " | ".join(all_text_pool).lower()
                    # Quota: daily / billing-period / subscription exhaustion
                    QUOTA_HINTS = (
                        "quota",
                        "daily limit",
                        "subscription",
                        "upgrade to",
                        "exhausted",
                        "已达",
                        "上限",
                        "超出今日",
                        "今日上限",
                        "配额",
                        "用完",
                        "用尽",
                    )
                    # Rate-limit: hourly / temporary throttle (resets in minutes)
                    RATE_HINTS = (
                        "rate limit",
                        "rate-limit",
                        "try again",
                        "too many",
                        "throttle",
                        "wait a moment",
                        "wait a few",
                        "limit reached",
                        "minute",
                        "稍后再试",
                        "请稍候",
                        "请稍后",
                        "请等待",
                        "频率",
                        "频次",
                        "太多",
                        "限次",
                        "稍候",
                        "稍后",
                        "分钟",
                    )
                    if any(s in combined_text for s in QUOTA_HINTS):
                        raise GrokQuotaExceededError(
                            f"create_image: submit button is disabled and "
                            f"the page indicates quota exhaustion. Stop "
                            f"generating until quota resets. "
                            f"banners={banners!r}, "
                            f"candidate_messages={candidate_messages!r}"
                        )
                    if any(s in combined_text for s in RATE_HINTS):
                        raise GrokRateLimitError(
                            f"create_image: submit button is disabled "
                            f"(rate-limited). banners={banners!r}, "
                            f"candidate_messages={candidate_messages!r}. "
                            f"Wait several minutes before retrying."
                        )
                    # Submit disabled with no matching keyword in either
                    # the narrow banners pool or the wide candidate_messages
                    # pool — preflight content moderation, account flag,
                    # or a Grok message wording we don't recognize yet.
                    # Surface BOTH pools so the maintainer can extend the
                    # hint dictionaries without instrumenting a separate
                    # probe script.
                    raise GrokAPIError(
                        f"create_image: submit button is disabled and no "
                        f"new generation jobs are arriving. Probe state: "
                        f"submit_aria={submit_state.get('submit_aria')!r}, "
                        f"submit_text={submit_state.get('submit_text')!r}, "
                        f"rejected_candidates="
                        f"{submit_state.get('rejected_candidates')!r}, "
                        f"banners={banners!r}, "
                        f"candidate_messages={candidate_messages!r}. "
                        f"Generation cannot proceed — this is usually a "
                        f"server-side block (preflight content moderation, "
                        f"account flag, or limit hit with a banner the "
                        f"connector doesn't recognize). The connector is "
                        f"failing fast rather than scrolling indefinitely.\n\n"
                        f"=== HELP US IMPROVE ===\n"
                        f"If you saw a rate-limit / quota / wait banner in "
                        f"the browser, please don't close the chrome window, "
                        f"then run from the connector repo:\n"
                        f"    python -m scripts.dump_grok_banner\n"
                        f"and forward workbench/grok_banner_dump.txt to the "
                        f"connector maintainer. The dump includes the actual "
                        f"banner text + selectors so the next release can "
                        f"classify this state as a typed "
                        f"GrokRateLimitError / GrokQuotaExceededError "
                        f"automatically."
                    )

                # Submit is still active → genuine rate-limit, just slow.
                # Wait longer before next scroll (15s, 30s, capped at 60s).
                backoff_wait = min(15 * (2 ** (consecutive_no_new_jobs - 3)), 60)
                logger.info(
                    f"[scroll] rate-limited but submit still enabled, "
                    f"waiting {backoff_wait}s before next scroll"
                )
                await asyncio.sleep(backoff_wait)

            # Scroll down to trigger more generation.
            #
            # Grok's infinite-scroll loader listens for TRUSTED wheel
            # events on the gallery container. ``container.scrollTop =
            # scrollHeight`` from JS moves the scrollbar but does not
            # fire a wheel event, so the sentinel/IntersectionObserver
            # that mounts more jobs never triggers — the loader stays
            # silent and new gens never start. Use CDP
            # Input.dispatchMouseEvent with type=mouseWheel for
            # isTrusted=true events.
            scroll_anchor_raw = await self._tab.evaluate(
                r"""
                JSON.stringify((() => {
                    const c = document.querySelector('.overflow-scroll') ||
                              document.querySelector('[class*="overflow-scroll"]') ||
                              document.querySelector('main');
                    if (!c) return null;
                    const r = c.getBoundingClientRect();
                    const vx = Math.max(0, r.x);
                    const vy = Math.max(0, r.y);
                    const vw = Math.min(window.innerWidth, r.x + r.width) - vx;
                    const vh = Math.min(window.innerHeight, r.y + r.height) - vy;
                    if (vw <= 0 || vh <= 0) return null;
                    return {
                        x: Math.round(vx + vw / 2),
                        y: Math.round(vy + vh / 2),
                    };
                })())
                """
            )
            import json as _json_scroll

            anchor = (
                _json_scroll.loads(scroll_anchor_raw)
                if isinstance(scroll_anchor_raw, str)
                else scroll_anchor_raw
            )
            if not anchor:
                # Fallback: viewport centre.
                anchor = {"x": 640, "y": 400}
            # Fire wheel events until we reach the bottom (or give up
            # after N). Each wheel ~800px; modern galleries need several.
            for _ in range(12):
                await self._tab.send(
                    cdp.input_.dispatch_mouse_event(
                        type_="mouseWheel",
                        x=float(anchor["x"]),
                        y=float(anchor["y"]),
                        delta_x=0.0,
                        delta_y=800.0,
                        pointer_type="mouse",
                    )
                )
                await asyncio.sleep(0.15)
                at_bottom = await self._tab.evaluate(
                    r"""
                    (() => {
                        const c = document.querySelector('.overflow-scroll') ||
                                  document.querySelector('[class*="overflow-scroll"]') ||
                                  document.querySelector('main');
                        if (!c) return true;
                        return (c.scrollTop + c.clientHeight) >= (c.scrollHeight - 150);
                    })()
                    """
                )
                if at_bottom:
                    break
            await asyncio.sleep(3 * d)  # Brief wait for scroll to trigger new jobs
            scroll_count += 1

        # Build result
        images = list(captured_data["jobs"].values())
        selected_post_ids: list[str] = []

        # Step 8: Collect post_ids via thumbnail_selector callback
        # When user clicks "Create Video" on a gallery image, Grok auto-favorites it
        # by sending POST /rest/media/post/like with {"id": "post_id"}
        # We capture these requests to get post_ids without navigation
        logger.info(
            f"[create_image] thumbnail_selector={thumbnail_selector is not None}, "
            f"images={len(images)} → will {'enter' if thumbnail_selector and images else 'skip'} capture block"
        )
        if thumbnail_selector and images:
            await asyncio.sleep(2 * d)  # Wait for DOM to settle

            # Set up request capture for /rest/media/post/like
            captured_like_ids: list[str] = []

            # Expose the running count to the selector so it can verify
            # its clicks actually fire persist requests, and retry.
            self._captured_persist_count = lambda: len(captured_like_ids)  # noqa: E731

            # 2026-04 UI POSTs /rest/media/post/create with body carrying
            # the gallery image's public URL; the RESPONSE is the new
            # post_id. Legacy UI POSTed /rest/media/post/like with id in
            # the request body. Support both: track req_ids on
            # RequestWillBeSent, extract id from whichever side carries
            # it on LoadingFinished.
            _pending_req_ids: set[str] = set()
            _request_bodies: dict[str, str] = {}

            async def on_persist_request(event: cdp.network.RequestWillBeSent):
                url = event.request.url
                if "/rest/media" in url:
                    logger.info(f"[post_id_capture] req seen: {url}")
                if MEDIA_POST_CREATE_ENDPOINT not in url and MEDIA_POST_LIKE_ENDPOINT not in url:
                    return
                _pending_req_ids.add(event.request_id)
                post_data = getattr(event.request, "post_data", None)
                if not post_data:
                    try:
                        r = await self._tab.send(
                            cdp.network.get_request_post_data(event.request_id)
                        )
                        post_data = r
                    except Exception:
                        pass
                if post_data:
                    _request_bodies[event.request_id] = post_data
                logger.info(
                    f"[post_id_capture] tracking {url} req_id={event.request_id} "
                    f"req_body={(post_data or '')[:150]!r}"
                )

            async def on_persist_finished(event: cdp.network.LoadingFinished):
                if event.request_id not in _pending_req_ids:
                    return
                logger.info(f"[post_id_capture] finished req_id={event.request_id}")
                _pending_req_ids.discard(event.request_id)
                # Try response body first (2026-04 /create path)
                body = ""
                try:
                    body_result = await self._tab.send(
                        cdp.network.get_response_body(request_id=event.request_id)
                    )
                    body = body_result[0] if isinstance(body_result, tuple) else str(body_result)
                except Exception as e:
                    logger.warning(f"[post_id_capture] response body fetch failed: {e}")
                logger.info(f"[post_id_capture] response body preview: {body[:300]!r}")
                # Fall back to request body (legacy /like path)
                req_body = _request_bodies.pop(event.request_id, "")

                import re

                post_id = None
                for src in (body, req_body):
                    if not src:
                        continue
                    try:
                        data = json_mod.loads(src)
                    except Exception:
                        data = None
                    if isinstance(data, dict):
                        post_id = (
                            data.get("id")
                            or data.get("postId")
                            or (data.get("post") or {}).get("id")
                        )
                    if not post_id:
                        m = re.search(r'"(?:postId|id)"\s*:\s*"([0-9a-f-]{36})"', src)
                        if m:
                            post_id = m.group(1)
                    if post_id:
                        break

                if post_id and post_id not in captured_like_ids:
                    captured_like_ids.append(post_id)
                    logger.info(f"[post_id_capture] Captured post_id: {post_id}")

            self._tab.add_handler(cdp.network.RequestWillBeSent, on_persist_request)
            self._tab.add_handler(cdp.network.LoadingFinished, on_persist_finished)

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

        # Post-generation aspect-ratio verification. Probe the natural
        # dimensions of the first non-moderated image and compare to the
        # ratio implied by `aspect_label`. Mismatch >5% → loud warning.
        # Two known causes: (a) UI manipulation silently failed (caught
        # above by no-btn / aspect-not-found), (b) Grok backend chose to
        # override (visual-layout-driven, observed for multi-actor scenes).
        try:
            await self._verify_aspect_ratio(images, requested=aspect_label)
        except Exception as _e:
            logger.debug(f"[create_image] aspect verification skipped: {_e}")

        # Gallery is ephemeral as of 2026-05 — clears immediately after
        # generation. Callers that rely on the post-gen browse-and-heart
        # human workflow need to use auto_favorite= or thumbnail_selector=
        # instead. Hint once per client.
        if not auto_favorite and thumbnail_selector is None and not self._gallery_ephemeral_hinted:
            logger.info(
                "[create_image] Generation complete. NOTE: Grok's frontend "
                "now clears the gallery DOM state immediately after the "
                "last image lands (regression observed 2026-05). The "
                "image_url fields in the result remain durable on Grok's "
                "CDN for hours, so post-process flows (download + filter) "
                "still work. But the manual 'browse the gallery in the "
                "browser and click hearts' workflow no longer works — "
                "pass auto_favorite=N to persist the first N images "
                "as posts, or thumbnail_selector=signal_file_selector(...) "
                "for human-in-the-loop. Hint fires once per client."
            )
            self._gallery_ephemeral_hinted = True

        return ImageGenerationResult(
            prompt=prompt,
            images=images,
            conversation_id=None,  # Not available via WebSocket
            selected_post_ids=selected_post_ids,
        )

    async def _probe_submit_state(self) -> dict:
        """Snapshot the Imagine composer's submit button + nearby banners.

        Used by ``create_image``'s scroll loop to distinguish "Grok is
        rate-limiting but submit still works" (worth backing off) from
        "Grok server-side disabled the submit button" (no new jobs ever
        coming — fail fast).

        Strict-only matcher: the button's aria-label OR innerText must
        be one of {submit, 提交, send, 发送} (case-insensitive, exact).
        ``generate`` / ``生成`` are explicitly NOT matched — those label
        mode-toggle buttons (e.g. 生成视频 = switch-to-video-mode toggle)
        which have independent disabled state from the actual submit
        action. v0.19.16 used a loose regex and got false-matched onto
        the mode toggle, missing the real submit button. If strict
        matching finds nothing, ``submit_found=False`` is returned — the
        caller's backoff path then runs as before (safer than guessing).

        Returns dict with keys:
          - ``submit_found`` (bool)
          - ``submit_disabled`` (bool | None) — None if no candidate found
          - ``submit_aria`` / ``submit_text`` — for diagnostics
          - ``banners`` (list[str]) — visible alert/toast/banner text
          - ``rejected_candidates`` (list) — visible non-submit buttons
            whose label fell into the looser pool, for diagnostics if
            Grok renames the submit button.
        """
        raw = await self._tab.evaluate(
            r"""
            (() => {
                // Strict actual-submit-action names (case-insensitive
                // exact match on aria-label OR innerText). NEVER match
                // "generate"/"生成" — those are mode toggles
                // (e.g. 生成视频 = "switch to video-gen mode"), not the
                // composer's submit action.
                const STRICT = new Set(['submit', '提交', 'send', '发送']);
                const norm = s => (s || '').trim().toLowerCase();
                const buttons = Array.from(document.querySelectorAll('button'));
                const visible = buttons.filter(b => {
                    const r = b.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                const isSubmit = b => STRICT.has(norm(b.getAttribute('aria-label')))
                                   || STRICT.has(norm(b.innerText));
                const submit = visible.find(isSubmit) || null;
                // Diagnostic: list visible buttons whose label MIGHT have
                // been a submit candidate under a looser matcher, so we
                // can spot Grok UI renames (e.g. 提交 → 发送提交).
                const looseRe = /submit|提交|send|发送|generate|生成/i;
                const rejected_candidates = visible
                    .filter(b => b !== submit)
                    .filter(b => {
                        const al = b.getAttribute('aria-label') || '';
                        const tx = b.innerText || '';
                        return looseRe.test(al + ' ' + tx);
                    })
                    .map(b => ({
                        aria: (b.getAttribute('aria-label') || '').trim(),
                        text: (b.innerText || '').trim().slice(0, 30),
                        disabled: !!b.disabled,
                    }))
                    .slice(0, 5);

                // Nearby banner / toast / alert text. Grok shows
                // rate-limit / quota messages here. Wide selector net —
                // we can't predict which class/role Grok uses for each
                // type of message, so try every plausible attachment.
                const BANNER_SELECTORS = [
                    '[role="alert"]', '[role="status"]', '[role="tooltip"]',
                    '[role="dialog"]', '[role="banner"]',
                    '[class*="toast" i]', '[class*="banner" i]',
                    '[class*="notification" i]', '[class*="error" i]',
                    '[class*="alert" i]', '[class*="message" i]',
                    '[class*="popover" i]', '[class*="warning" i]',
                    '[class*="hint" i]', '[class*="dialog" i]',
                    '[class*="tooltip" i]',
                ];
                const banners = Array.from(document.querySelectorAll(
                    BANNER_SELECTORS.join(', ')
                ))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                })
                .map(el => (el.innerText || '').trim())
                .filter(t => t && t.length > 0 && t.length < 300)
                .slice(0, 8);

                // Last-resort wide net: ANY visible text node containing
                // a rate-limit / quota / wait keyword. Catches the case
                // where Grok shows the message via a plain <div> with no
                // semantic class. The match keys here MUST be a strict
                // superset of what the Python side then classifies on,
                // so we never silently miss text the classifier would
                // recognize. Surfaced into GrokAPIError when fail-fast
                // triggers — caller (or maintainer) can read what Grok
                // actually said without separate instrumentation.
                const KEYWORDS = [
                    'rate', 'limit', 'quota', 'try again', 'too many',
                    'wait', 'throttle', 'exhaust', 'exceed', 'retry',
                    'reach', 'maximum', 'upgrade', 'subscription',
                    '稍后', '稍候', '请等', '请稍', '频率', '频次',
                    '限制', '限次', '上限', '已达', '太多', '超出',
                    '用完', '用尽', '配额', '额度', '分钟', '小时',
                    'minute', 'minutes', 'hour', 'hours', 'second',
                ];
                const kw_re = new RegExp(
                    KEYWORDS.map(k =>
                        k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
                    ).join('|'),
                    'i'
                );
                const candidate_messages = [];
                try {
                    const walker = document.createTreeWalker(
                        document.body,
                        NodeFilter.SHOW_TEXT,
                        null
                    );
                    let n;
                    let count = 0;
                    while ((n = walker.nextNode()) && count < 12) {
                        const t = (n.textContent || '').trim();
                        if (t.length < 4 || t.length > 220) continue;
                        if (!kw_re.test(t)) continue;
                        const parent = n.parentElement;
                        if (!parent) continue;
                        const r = parent.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        candidate_messages.push({
                            text: t.slice(0, 200),
                            tag: parent.tagName,
                            cls: (parent.className || '').toString().slice(0, 60),
                            role: parent.getAttribute('role') || '',
                        });
                        count++;
                    }
                } catch (e) { /* ignore walker errors */ }

                return JSON.stringify({
                    submit_found: !!submit,
                    submit_disabled: submit ? !!submit.disabled : null,
                    submit_aria: submit
                        ? (submit.getAttribute('aria-label') || '').trim()
                        : null,
                    submit_text: submit
                        ? (submit.innerText || '').trim().slice(0, 40)
                        : null,
                    rejected_candidates,
                    banners,
                    candidate_messages,
                });
            })()
            """
        )
        try:
            return json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError):
            return {}

    async def _verify_aspect_ratio(self, images, *, requested: str) -> None:
        """Compare returned image dims to the requested aspect ratio.

        Probes the first non-moderated image via in-browser ``Image()``
        loader (no extra fetch — uses Grok's CDN cache), parses the
        requested label (e.g. ``"9:16"``, ``"2:3"``, ``"portrait"``),
        and emits a ``logger.warning`` when the actual ratio diverges
        from requested by more than 5%. No-op on missing images or
        unparsable label.
        """
        # Parse requested ratio into a float (w/h).
        ratio_aliases = {
            "portrait": 2 / 3,
            "landscape": 3 / 2,
            "square": 1.0,
        }
        if requested in ratio_aliases:
            target_ratio = ratio_aliases[requested]
        elif ":" in (requested or ""):
            try:
                w, h = requested.split(":")
                target_ratio = float(w) / float(h)
            except (ValueError, ZeroDivisionError):
                return
        else:
            return  # Unknown / unparsable

        first_url = None
        for img in images:
            if hasattr(img, "moderated") and getattr(img, "moderated", False):
                continue
            if isinstance(img, dict) and img.get("moderated"):
                continue
            url = (
                getattr(img, "image_url", None)
                if hasattr(img, "image_url")
                else img.get("image_url")
                if isinstance(img, dict)
                else None
            )
            if url:
                first_url = url
                break
        if not first_url:
            return

        dims_raw = await self._tab.evaluate(
            r"""
            (async (url) => {
                return new Promise((resolve) => {
                    const img = new Image();
                    img.onload = () => resolve(JSON.stringify({
                        w: img.naturalWidth, h: img.naturalHeight,
                    }));
                    img.onerror = () => resolve(JSON.stringify({}));
                    img.src = url;
                    setTimeout(() => resolve(JSON.stringify({})), 6000);
                })
            })(__URL__)
            """.replace("__URL__", json.dumps(first_url)),
            await_promise=True,
        )
        try:
            dims = json.loads(dims_raw) if isinstance(dims_raw, str) else {}
        except Exception:
            dims = {}
        w, h = dims.get("w"), dims.get("h")
        if not w or not h:
            return
        actual_ratio = w / h
        # 5% tolerance — covers Grok's slight per-pixel rounding without
        # masking real orientation flips (1.49 vs 0.67 is >100% off).
        if abs(actual_ratio - target_ratio) / target_ratio > 0.05:
            actual_orient = (
                "landscape" if actual_ratio > 1 else "portrait" if actual_ratio < 1 else "square"
            )
            req_orient = (
                "landscape" if target_ratio > 1 else "portrait" if target_ratio < 1 else "square"
            )
            logger.warning(
                "[create_image] aspect_ratio mismatch: requested %r "
                "(target ratio=%.3f, %s) but received %dx%d (actual "
                "ratio=%.3f, %s). Common causes: "
                "(1) the aspect-ratio UI button silently failed (check "
                "for earlier '宽高比 not found' warnings); "
                "(2) Grok backend overrode the request (visual-layout-"
                "driven for multi-actor / horizontal-composition prompts). "
                "If (2), Grok may not honor aspect_ratio for this prompt; "
                "rephrase or accept the override. Connector did pass the "
                "requested value via UI; check connector logs above to "
                "confirm UI step succeeded.",
                requested,
                target_ratio,
                req_orient,
                w,
                h,
                actual_ratio,
                actual_orient,
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
            # txt2vid has no image source; the video post IS the
            # top-level post, so source and parent both point at it.
            source_post_id=post_id,
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
            params: Dict with keys from VIDEO_KEYS (see grok_web.schema).
                The per-key descriptions below are generated from
                ``grok_web.schema.PARAMS`` (SSOT) — edit there.

                <SCHEMA_ARGS>

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

            Recommended: pass ``verify_final=True`` whenever you care
            about the final verdict (most callers). An empirical 5x5
            A/B run over borderline content saw the second-stage verdict
            flip moderated=False -> True on 20% of passes; relying on
            the immediate field alone silently accepts those.

            Alternative: call ``client.check_video_moderated(video_id)``
            yourself at whatever point in the flow you need it.

        Transport-drop retries (upload2vid with borderline content):
            When Grok's anti-abuse engages (typically after a prior
            raised moderation error on the same client), the server may
            drop subsequent /app-chat/conversations/new XHRs at the
            transport layer. The connector surfaces this as::

                GrokAPIError("NDJSON body missing and REST recovery
                              failed: ...")

            This is a Grok-side rate limit, not a bug in your code.
            Empirically it fires on roughly half of second attempts
            during degraded periods. Retry the call (fresh client often
            helps, or wait a minute); all defensive timeouts are
            bounded so you get a clear error within ~30s rather than a
            hang.

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

            # video-extend: generate a continuation of an existing video
            ext = await client.create_video({
                "images": ["video:<video-uuid>"],
                "duration": "6s",
            })
            # ext.video_id is the new continuation; the source video's id
            # is recoverable via
            #   details = await client.get_post_details(post_parent_id)
            #   details.parent_of(ext.video_id)

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
        # Video gen defaults to 600s (not the shared schema default of 300).
        # img2vid under NSFW/queue pressure regularly hits progress<100 at
        # 300s, which returned a confusing partial result; 600 absorbs
        # those naturally. Callers that need shorter can still pass
        # ``timeout``; callers still hitting timeouts can use
        # ``wait_for_video_completion({'video_id': ..., 'timeout': ...})`` on
        # the returned ``in_progress`` result to resume without re-submitting.
        timeout = p.get("timeout", 600)

        # Normalize duration to int
        duration = p.get("duration", "10s")
        if isinstance(duration, str):
            duration = int(duration.replace("s", ""))

        resolution = p.get("resolution", "720p")
        preset = p.get("preset", "normal")
        wait_for_video = p.get("wait_for_video", True)
        verify_final = p.get("verify_final", False)
        preserve_fav = p.get("preserve_source_favorite_state", False)

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
                # img2vid — use first post source. Explicit
                # select_post + generate_video_from_current composition:
                # select guarantees Grok shows post_id (correcting any
                # SPA redirect to a descendant), then we generate from
                # the currently-selected view. Keeping the wrapper shape
                # matches the rest of the dict API while the underlying
                # primitives stay independently usable.
                post_id = sources[0][1]
                was_favorited = None
                if preserve_fav:
                    was_favorited = await self._get_favorited_state(post_id)
                elif not self._favorite_pollution_hinted:
                    logger.info(
                        "[create_video] Grok auto-favorites the source post "
                        "on each UI-driven img2vid call, which accumulates "
                        "duplicate entries in the user's favorites tab. "
                        "Pass preserve_source_favorite_state=True to have "
                        "the connector snapshot-and-revert the state (only "
                        "when the source was unambiguously not favorited "
                        "pre-call — safe default). Hint fires once per client."
                    )
                    self._favorite_pollution_hinted = True
                await self.select_post({"post_id": post_id})
                result = await self.generate_video_from_current(
                    source_post_id=post_id,
                    preset=preset,
                    timeout=timeout,
                    adjustment_prompt=prompt if prompt else None,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                )
                if preserve_fav and was_favorited is False:
                    try:
                        await self.unfavorite_post(post_id)
                    except Exception as _e:
                        logger.warning(
                            f"[create_video] could not restore favorite state for {post_id}: {_e}"
                        )
            elif kind == "video":
                # video-extend — generate a continuation from an existing
                # Grok video post. UI flow: navigate to the video page,
                # click 扩展, optionally enter filmstrip mode to select a
                # seed frame, then click 生成视频.
                # Only the first 'video:' ref is used (Grok extends one
                # video at a time).
                if len(sources) > 1:
                    logger.warning(
                        "create_video({'images': ['video:...', 'video:...']}) "
                        "only extends the first video; additional refs ignored."
                    )
                src_vid = sources[0][1]
                # Pass through the original duration string (extend_video
                # expects '6s'/'10s', not the int we normalized earlier).
                orig_duration = params.get("duration")
                extend_res = await self.extend_video(
                    {
                        "video_id": src_vid,
                        "seed_start": p.get("seed_start"),
                        "duration": orig_duration,
                        "prompt": prompt if prompt else None,
                        "timeout": timeout,
                        "preserve_source_favorite_state": preserve_fav,
                    }
                )
                # extend_video returns VideoExtendResult; adapt its fields
                # so callers receive a normal VideoGenerationResult. Seed
                # bookkeeping stays on the VideoExtendResult — callers who
                # need it should call client.extend_video() directly.
                result = VideoGenerationResult(
                    video_id=extend_res.video_id,
                    # For video-extend via dict API, the source post is
                    # the video we extended from (same as parent_post_id).
                    source_post_id=extend_res.source_video_id,
                    parent_post_id=extend_res.parent_post_id,
                    moderated=extend_res.moderated,
                    progress=extend_res.progress,
                    mode=extend_res.mode,
                    model_name=extend_res.model_name,
                    conversation_id=extend_res.conversation_id,
                    statsig_id=extend_res.statsig_id,
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

        # Enrich result with authoritative output duration from Grok's
        # post metadata. Skip when moderated — no durable post exists
        # to look up — or when the underlying path already populated
        # (txt2vid pre-wait can return before the post is indexed, in
        # which case fetch will yield None and we leave it unset).
        if not result.moderated and result.video_id and result.duration_s is None:
            dur_s, _ = await self._fetch_video_duration(result.video_id)
            if dur_s is not None:
                result.duration_s = dur_s

        # is_persisted probe: confirm video_id resolves to a real post.
        # Mainly catches the moderated-NSFW case where Grok's NDJSON
        # streams a videoId that's just a per-stream identifier — no
        # real post is persisted. Without this signal the caller has
        # to discover the bogus id by their own get_post_details call
        # 404'ing. ~150ms cost; best-effort, never hard-fails the result.
        if result.video_id:
            try:
                await self.get_post_details(result.video_id)
                result.is_persisted = True
            except GrokNotFoundError:
                result.is_persisted = False
            except Exception:
                # Network / auth blip — leave None ("unknown"); caller
                # can decide whether to retry the check.
                pass

        return result

    async def generate_video_from_current(
        self,
        source_post_id: str,
        preset: str = "normal",
        timeout: int = 300,
        stable_id: str | None = None,
        adjustment_prompt: str | None = None,
        duration: int = 10,
        resolution: str = "720p",
        aspect_ratio: str = "2:3",
        thumbnail_index: int | None = None,
    ) -> VideoGenerationResult:
        """Generate a video from the image post currently selected on the tab.

        Lower-level primitive: opens the settings gear menu to configure
        video options (duration / resolution / aspect ratio), clicks
        "制作视频" on the image overlay, optionally enters a prompt for
        custom mode, and waits for the NDJSON generation response.

        Does NOT navigate. Caller is responsible for selecting the
        source image first via :meth:`select_post` — ``source_post_id``
        is only used for result labeling, retry thumbnail-matching,
        and the legacy ``stable_id`` reload path. If the tab is not on
        ``source_post_id``'s view, the UI click either fails (button
        not found → retried internally) or operates on the wrong post.

        See :meth:`create_video` for the dict-API wrapper that composes
        ``select_post + generate_video_from_current`` — use that for the
        common case.

        Args:
            source_post_id: The image post UUID to generate from. Used
                for result metadata (``source_post_id`` / ``parent_post_id``)
                and as the anchor for internal retry-recovery if Grok's
                UI drifts mid-flow.
            preset: 'normal', 'fun', or 'spicy'.
            timeout: Max seconds to wait for the generation response.
            stable_id: Optional custom stable_id to inject before generation.
            adjustment_prompt: Video prompt (triggers 'custom' mode). None
                keeps Grok's default preset mode.
            duration: 6 or 10 seconds.
            resolution: "480p" or "720p".
            aspect_ratio: "2:3" / "3:2" / "1:1" / "9:16" / "16:9".
            thumbnail_index: If the source has multiple images, pick one
                by index before generating.

        Returns:
            VideoGenerationResult with video_id (empty if moderated).
        """
        import asyncio

        from ai_dev_browser import cdp

        # Inject custom stable_id if provided
        if stable_id:
            await self.set_stable_id(stable_id, reload_page=False)

        # Internal alias to keep the body untouched. This is the post we
        # anchor result metadata and retry-recovery to; navigation to it
        # happened in the caller via select_post.
        parent_post_id = source_post_id

        # Normalize preset to string
        preset_str = str(preset).lower()

        # Map preset string to menu text (case-sensitive as shown in UI)
        preset_menu_map = {
            "normal": "Normal",
            "fun": "Fun",
            "spicy": "Spicy",
        }
        preset_menu_text = preset_menu_map.get(preset_str, "Normal")

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

        async def _restore_image_view() -> bool:
            """Click the sidebar thumbnail matching parent_post_id so the
            page renders the image view (not a descendant video).

            Grok's post page silently redirects to the tail descendant
            when one exists; a post that was a brand-new edit_image
            output can also land on the wrong sibling candidate
            depending on Grok's default selection. Calling this before
            and between 制作视频 retries makes the lookup resilient to
            both.

            Returns True if a matching thumbnail was clicked.
            """
            return bool(
                await self._tab.evaluate(
                    r"""
                    (() => {
                        const want = "__POST_ID__";
                        const imgs = Array.from(document.querySelectorAll('img'))
                            .filter(i => {
                                const r = i.getBoundingClientRect();
                                if (r.width < 20 || r.width > 150) return false;
                                return (i.currentSrc || i.src || '').includes(want);
                            });
                        if (imgs.length === 0) return false;
                        let el = imgs[0];
                        while (el && el.tagName !== 'BUTTON' && el.parentElement) {
                            el = el.parentElement;
                        }
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const x = r.x + r.width/2, y = r.y + r.height/2;
                        const o = {bubbles: true, cancelable: true, clientX: x, clientY: y,
                                   pointerType: 'mouse', button: 0, pointerId: 1, isPrimary: true};
                        el.dispatchEvent(new PointerEvent('pointerdown', o));
                        el.dispatchEvent(new MouseEvent('mousedown', o));
                        el.dispatchEvent(new PointerEvent('pointerup', o));
                        el.dispatchEvent(new MouseEvent('mouseup', o));
                        el.dispatchEvent(new MouseEvent('click', o));
                        return true;
                    })()
                    """.replace("__POST_ID__", parent_post_id)
                )
            )

        async def _click_make_video_button() -> bool:
            """Click the '制作视频' or '生成视频' button to trigger generation.

            If the expected button isn't present, try restoring image
            view once before giving up — the post page may have drifted
            off the image (Grok can default to a sibling edit candidate
            or a descendant video depending on state).
            """
            # Try "制作视频" first (initial image post state)
            btn = await self._tab.query_selector('button[aria-label="制作视频"]')
            if not btn:
                btn = await self._tab.query_selector('button[aria-label="Make video"]')
            # Fallback: "生成视频" (video mode state, arrow up = regenerate)
            if not btn:
                btn = await self._tab.query_selector('button[aria-label="生成视频"]')
            if not btn:
                btn = await self._tab.query_selector('button[aria-label="Generate video"]')
            # Defensive: we may have landed on a sibling or descendant
            # view where the button doesn't exist. Restore image view
            # explicitly and try once more.
            if not btn and await _restore_image_view():
                await asyncio.sleep(1.5)
                btn = await self._tab.query_selector('button[aria-label="制作视频"]')
                if not btn:
                    btn = await self._tab.query_selector('button[aria-label="Make video"]')
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
                    # Last-ditch: moderation state or cached DOM can stick
                    # across soft navigations in long retry loops (reported
                    # to fail deterministically on the 4th consecutive call
                    # with moderated outputs). Force a hard reload and try
                    # one more time before giving up.
                    logger.warning(
                        "[generate_video_from_current] '制作视频' not found "
                        "after %d retries — forcing hard page_reload as "
                        "last-ditch and retrying once.",
                        max_click_retries,
                    )
                    try:
                        await self._tab.page_reload()
                        await asyncio.sleep(3 + random.uniform(0, 1.0))
                        if await _click_make_video_button() and await _wait_for_request(
                            click_wait_timeout
                        ):
                            break
                    except Exception as _e:
                        logger.debug(f"page_reload last-ditch failed: {_e}")
                    raise GrokAPIError(
                        "Could not find '制作视频' button after retries "
                        "(including hard page reload). Common causes:\n"
                        "  1. Consecutive moderated generations (3+) can leave "
                        "persistent UI state — the connector's banner-killer "
                        "now tries to dismiss moderation alerts/toasts; if "
                        "you still hit this, inspect the DOM for a new "
                        "overlay pattern and report back.\n"
                        "  2. The target post belongs to another user (no "
                        "generate permission).\n"
                        "  3. The target is not an image post (already a "
                        "video / deleted)."
                    )
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

    async def download_video(self, params, *legacy_args, **legacy_kwargs) -> Path:
        """Download a video to local file.

        Canonical shape (v0.19.0+) — dict-style::

            await client.download_video({
                "video_id": "abc-123",
                "output_path": "out.mp4",
                "prefer_hd": True,
            })

        Args:
            params: Dict with keys from DOWNLOAD_KEYS (see grok_web.schema).
                ``video_id`` and ``output_path`` are required. Per-key
                descriptions below are generated from
                ``grok_web.schema.PARAMS`` (SSOT).

                <SCHEMA_ARGS>

        Returns:
            Path to the downloaded file (same as ``output_path``).

        Raises:
            GrokNotFoundError: If video not found.
            GrokAPIError: If all fetch strategies fail (e.g. 403 signed
                URL rejected all credential modes).
            TypeError: If ``params`` is not a dict (or, for the
                deprecated positional form, ``video_id`` + ``output_path``
                strings), or if a required key is missing.

        Legacy form (deprecated v0.19.0, removed v0.20.0)::

            await client.download_video(video_id, output_path,
                                        prefer_hd=True)

        Still works but emits ``DeprecationWarning``. The
        ``parent_post_id`` kwarg is also accepted for backwards
        compatibility but ignored — the direct-lookup path no longer
        needs it.
        """
        import warnings

        from .schema import DOWNLOAD_KEYS, validate_params

        if isinstance(params, str):
            warnings.warn(
                "download_video(video_id, output_path, ...) positional/kwarg "
                "form is deprecated since v0.19.0; use "
                "download_video({'video_id': ..., 'output_path': ...}) for "
                "consistency with create_video / edit_image. Removed in "
                "v0.20.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            normalized = {"video_id": params}
            if legacy_args:
                # First positional after video_id was output_path.
                normalized["output_path"] = legacy_args[0]
                if len(legacy_args) > 1:
                    raise TypeError(
                        "download_video: too many positional args (legacy "
                        f"signature accepts at most 2, got {1 + len(legacy_args)})"
                    )
            # parent_post_id was a deprecated no-op even on the legacy
            # signature; warn-and-drop rather than rejecting the call.
            if "parent_post_id" in legacy_kwargs:
                legacy_kwargs.pop("parent_post_id")
                warnings.warn(
                    "download_video(parent_post_id=...) has been a no-op "
                    "since the direct-lookup path landed; the kwarg is "
                    "ignored and will be removed in v0.20.0.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            normalized.update(legacy_kwargs)
            params = normalized
        elif isinstance(params, dict):
            if legacy_args or legacy_kwargs:
                raise TypeError(
                    "download_video: cannot mix dict params and "
                    "positional/kwargs. Pass everything inside the dict."
                )
        else:
            raise TypeError(
                f"download_video: first arg must be a dict (canonical) or "
                f"a video_id str (deprecated), got {type(params).__name__}"
            )

        p = validate_params(params, DOWNLOAD_KEYS)
        video_id = p.get("video_id")
        output_path = p.get("output_path")
        if not video_id:
            raise TypeError("download_video: 'video_id' is required in params dict")
        if not output_path:
            raise TypeError("download_video: 'output_path' is required in params dict")
        prefer_hd = p.get("prefer_hd", True)

        output_path = Path(output_path)

        video_url: str | None = None

        # Strategy 1: fetch the video post directly. This is the most
        # reliable path — get_post_details(video_id) always returns that
        # post's own mediaUrl / hdMediaUrl when the video exists,
        # regardless of how Grok linked it in the chain.
        try:
            details = await self.get_post_details(video_id)
            candidate = None
            if prefer_hd:
                candidate = details.hd_media_url
            if not candidate:
                candidate = details.media_url
            if candidate:
                video_url = candidate
                logger.debug(
                    "[download_video] direct lookup found URL for %s via get_post_details",
                    video_id,
                )
        except GrokNotFoundError:
            pass

        # Strategy 2: favorites scan (legacy slow path) — only when the
        # direct lookup didn't surface the URL. Iterates the user's
        # favorited parents and looks for video_id among their children.
        if not video_url:
            posts = await self.list_posts(limit=100, source="favorites")
            for post in posts:
                try:
                    details = await self.get_post_details(post.id)
                except GrokNotFoundError:
                    continue
                for child in details.children:
                    if child.id == video_id:
                        candidate = (child.hd_media_url if prefer_hd else None) or child.media_url
                        if candidate:
                            video_url = candidate
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


# =============================================================================
# Docstring SSOT splicing — replaces <SCHEMA_ARGS> markers in method
# docstrings with the Args block generated from schema.PARAMS. Running this
# at module load means `help(GrokClient.create_video)` and IDE tooltips show
# the same per-parameter descriptions that live in schema.py.
#
# Adding a new param is a one-line edit in schema.PARAMS + the relevant
# *_KEYS list — the docstring updates automatically.
# =============================================================================


def _splice_all_docstrings() -> None:
    from .schema import (
        DOWNLOAD_KEYS,
        EDIT_KEYS,
        EXTEND_KEYS,
        IMAGE_KEYS,
        SELECT_POST_KEYS,
        UPLOAD_KEYS,
        VIDEO_KEYS,
        WAIT_FOR_COMPLETION_KEYS,
        splice_schema_into_docstring,
    )

    for method_name, keys in [
        ("create_video", VIDEO_KEYS),
        ("extend_video", EXTEND_KEYS),
        ("edit_image", EDIT_KEYS),
        ("create_image", IMAGE_KEYS),
        ("upload_images", UPLOAD_KEYS),
        ("download_video", DOWNLOAD_KEYS),
        ("select_post", SELECT_POST_KEYS),
        ("wait_for_video_completion", WAIT_FOR_COMPLETION_KEYS),
    ]:
        method = getattr(GrokClient, method_name, None)
        if method is None:
            continue
        method.__doc__ = splice_schema_into_docstring(method.__doc__, keys)


_splice_all_docstrings()
