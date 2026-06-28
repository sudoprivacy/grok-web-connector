"""
Grok Agent Mode Connector — GrokAgentClient

Browser automation client for Grok Imagine Agent Mode (infinite canvas + chat).
Peer to client.py (Grok Imagine browser) and xai_client.py (REST API).

Stateless with session resume: each call opens browser → navigates → acts →
extracts → closes. Conversation state persisted server-side by Grok.
Session URL returned for follow-up.

Public API:
    Use get_agent_client() from grok_web package — returns GrokAgentClient.
"""

import asyncio
import contextlib
import json as json_mod
import logging
from pathlib import Path
from typing import Any

from ai_dev_browser.core.config import DEFAULT_DEBUG_HOST

from .auth import DEFAULT_CONFIG_PATH, load_config, save_cookies
from .exceptions import GrokAPIError, GrokConfigError
from .models import AgentResponse, GrokCookies
from .schema import AGENT_KEYS, validate_params

logger = logging.getLogger(__name__)

# Named profile for Agent Mode Chrome (separate from Grok Imagine)
AGENT_CHROME_PROFILE = "grok-agent"

# Agent Mode base URL
AGENT_BASE_URL = "https://grok.com/imagine/agent"

# Chat input selectors (tried in order — validated 2026-06-28)
_INPUT_SELECTORS = [
    '[aria-label="Ask Grok anything"]',
    ".tiptap.ProseMirror",
    '[contenteditable="true"]',
]

# Submit button selectors (multi-locale — validated 2026-06-28)
# Agent Mode uses aria-label="提交" (Submit), NOT "发送" (Send)
_SUBMIT_SELECTORS = [
    '[aria-label="提交"]',
    '[aria-label="Submit"]',
    'button[type="submit"]',
]

# OneTrust cookie banner killer (same as GrokClient)
_BANNER_KILLER = """
(() => {
    const ot = document.getElementById('onetrust-consent-sdk');
    if (ot) ot.remove();
})()
"""


class GrokAgentClient:
    """Grok Imagine Agent Mode browser automation client.

    Uses Chrome DevTools Protocol (via ai-dev-browser) for Agent Mode
    operations. Mirrors GrokClient's lifecycle (constructor, async
    context manager, cookie auth).

    Usage:
        >>> async with GrokAgentClient() as agent:
        ...     r = await agent.send({"message": "create a logo for Bean Dream"})
        ...     print(r.text, r.image_urls, r.session_url)

        >>> # Or use get_agent_client() factory
        >>> from grok_web import get_agent_client
        >>> async with get_agent_client() as agent:
        ...     r = await agent.send({"message": "draw a red circle"})
    """

    def __init__(
        self,
        cookies: GrokCookies | None = None,
        config_path: Path | str | None = None,
        headless: bool = False,
        host: str | None = None,
        port: int | None = None,
        profile: str | None = None,
        startup_timeout: float = 30.0,
        extra_chrome_args: list[str] | None = None,
        user_data_dir: "str | Path | None" = None,
    ):
        """Initialize GrokAgentClient.

        Args:
            cookies: Pre-loaded GrokCookies (optional, loads from config if None)
            config_path: Path to config file (default: ~/.grok-config.json)
            headless: Run browser in headless mode (default: False)
            host: Chrome debugging host (optional, defaults to 127.0.0.1)
            port: Chrome debugging port (optional, auto-assigned if None)
            profile: Chrome profile name (default: "grok-agent")
            startup_timeout: Seconds to wait for Chrome to bind its debug port
            extra_chrome_args: Additional Chrome command-line flags
            user_data_dir: Absolute path for Chrome's --user-data-dir
        """
        self._provided_cookies = cookies
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

        self.cookies: GrokCookies | None = None
        self._headless = headless
        self._browser = None
        self._tab = None
        self._initialized = False

        self._remote_host = host or DEFAULT_DEBUG_HOST
        self._remote_port = port
        self._profile = profile
        self._startup_timeout = startup_timeout
        self._extra_chrome_args = list(extra_chrome_args) if extra_chrome_args else None
        self._user_data_dir = Path(user_data_dir).resolve() if user_data_dir else None

    # =========================================================================
    # Context manager — mirrors GrokClient's lifecycle
    # =========================================================================

    async def __aenter__(self):
        from ai_dev_browser import cdp
        from ai_dev_browser.core.connection import connect_browser

        # Patch Chrome stderr (same as GrokClient)
        from .client import _patch_ai_dev_browser_chrome_stderr

        _patch_ai_dev_browser_chrome_stderr()

        # Load cookies
        if self._provided_cookies is not None:
            self.cookies = self._provided_cookies
        else:
            self.cookies = await self._load_or_setup_cookies()

        # Launch or reuse Chrome (same pattern as GrokClient)
        from .client import GrokClient

        actual_port = self._remote_port
        try:
            profile_name = self._profile or AGENT_CHROME_PROFILE
            user_data_dir = self._user_data_dir or (
                Path.home() / ".grok-web-connector" / "profiles" / profile_name
            )
            user_data_dir.mkdir(parents=True, exist_ok=True)

            launcher = GrokClient.__new__(GrokClient)
            actual_port, reused = launcher._launch_or_reuse_chrome(
                user_data_dir=user_data_dir,
                requested_port=self._remote_port,
                headless=self._headless,
                extra_args=[
                    "--disable-logging",
                    "--log-file=NUL",
                    *(self._extra_chrome_args or []),
                ],
                force_new=False,
                startup_timeout=self._startup_timeout,
            )
            if reused:
                logger.info(f"Reusing Chrome on port {actual_port} (profile: {profile_name})")
            else:
                logger.info(f"Started Chrome on port {actual_port} (profile: {profile_name})")
        except (FileNotFoundError, TimeoutError, RuntimeError) as e:
            raise GrokAPIError(f"Chrome failed to start: {e}") from e

        self._remote_port = actual_port

        try:
            self._browser = await connect_browser(
                host=self._remote_host,
                port=actual_port,
            )
        except Exception as e:
            raise GrokAPIError(
                f"Failed to connect to Chrome on {self._remote_host}:{actual_port}: {e}"
            ) from e

        # Reuse existing grok.com/imagine/agent tab or first page tab
        self._tab = None
        try:
            targets = getattr(self._browser, "targets", None) or []
            page_targets = [t for t in targets if getattr(t, "type_", "") == "page"]
            for target in page_targets:
                url = getattr(target, "url", "") or ""
                if "grok.com/imagine/agent" in url:
                    self._tab = target
                    break
            if self._tab is None and page_targets:
                self._tab = page_targets[0]
        except Exception:
            pass

        if self._tab is None:
            self._tab = await self._browser.get("about:blank")

        # Inject cookies via CDP
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

        self._initialized = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None and self._initialized and self._browser:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._auto_save_cookies(), timeout=5.0)

        if self._tab:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._tab.disconnect(), timeout=5.0)

    # =========================================================================
    # Cookie management
    # =========================================================================

    async def _load_or_setup_cookies(self) -> GrokCookies:
        """Load cookies from config, or trigger interactive setup."""
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

    async def _auto_save_cookies(self) -> None:
        """Extract cookies from browser and save to config."""
        try:
            all_cookies = await self._browser.cookies.get_all()
            cookie_dict = {}
            required = {"sso", "sso-rw", "x-userid", "cf_clearance"}

            for cookie in all_cookies:
                domain = getattr(cookie, "domain", "")
                name = getattr(cookie, "name", "")
                value = getattr(cookie, "value", "")

                if ("grok.com" in domain or "x.ai" in domain) and name in required:
                    cookie_dict[name] = value

            if all(cookie_dict.get(name) for name in required):
                fresh_cookies = GrokCookies(**cookie_dict)
                save_cookies(fresh_cookies, self._config_path)
                logger.debug(f"Auto-saved cookies to {self._config_path}")
        except Exception as e:
            logger.debug(f"Failed to auto-save cookies: {e}")

    # =========================================================================
    # DOM helpers — selectors validated via e2e 2026-06-28
    # =========================================================================

    async def _evaluate_json(self, js_expr: str) -> Any:
        """Evaluate JS that returns JSON.stringify'd result.

        CDP returns complex objects as list-of-pairs, not dicts.
        Wrapping in JSON.stringify on the JS side and json.loads
        on the Python side guarantees proper dict/list structure.
        """
        try:
            raw = await self._tab.evaluate(js_expr)
        except Exception:
            return None
        if isinstance(raw, str):
            try:
                return json_mod.loads(raw)
            except (json_mod.JSONDecodeError, TypeError):
                return raw
        return raw

    async def _wait_for_selector(
        self,
        selector: str,
        timeout: float = 30.0,
        interval: float = 0.25,
    ) -> bool:
        """Poll for a CSS selector to match a visible DOM element."""
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

    async def _kill_banner(self) -> None:
        """Remove OneTrust cookie banner (blocks submit button coords)."""
        with contextlib.suppress(Exception):
            await self._tab.evaluate(_BANNER_KILLER)

    async def _fill_input(self, text: str) -> None:
        """Fill the chat input with text via ProseMirror execCommand."""
        escaped = text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")

        selector_js = " || ".join(f"document.querySelector('{s}')" for s in _INPUT_SELECTORS)
        fill_result = await self._tab.evaluate(
            f"""
            (() => {{
                const ed = {selector_js};
                if (!ed) return 'no-editor';
                ed.focus();
                document.execCommand('selectAll');
                document.execCommand('delete');
                document.execCommand('insertText', false, `{escaped}`);
                return 'ok';
            }})()
            """
        )
        if fill_result == "no-editor":
            raise GrokAPIError(
                "Could not find Agent Mode chat input. Tried selectors: "
                + ", ".join(_INPUT_SELECTORS)
            )

    async def _click_submit(self) -> None:
        """Click the submit/send button (aria-label="提交")."""
        for sel in _SUBMIT_SELECTORS:
            escaped = sel.replace("\\", "\\\\").replace("'", "\\'")
            clicked = await self._tab.evaluate(
                f"""
                (() => {{
                    const btn = document.querySelector('{escaped}');
                    if (btn && !btn.disabled) {{
                        btn.click();
                        return true;
                    }}
                    return false;
                }})()
                """
            )
            if clicked:
                return

        raise GrokAPIError(
            "Could not find or click submit button. Tried selectors: "
            + ", ".join(_SUBMIT_SELECTORS)
        )

    async def _select_agent_mode(self) -> None:
        """Ensure Agent mode radio is selected (for new conversations).

        Agent Mode page has radios: 图片 / 视频 / 代理.
        On /imagine/agent the 代理 radio is pre-selected.
        """
        await self._tab.evaluate(
            """
            (() => {
                const radios = document.querySelectorAll('[role="radio"]');
                for (const r of radios) {
                    const text = r.textContent || '';
                    if (text.includes('Agent') || text.includes('代理')) {
                        if (r.getAttribute('aria-checked') !== 'true') {
                            r.click();
                        }
                        return 'selected';
                    }
                }
                return 'not-found';
            })()
            """
        )

    async def _set_model(self, model: str) -> None:
        """Set the agent model via settings dropdown."""
        settings_clicked = await self._tab.evaluate(
            """
            (() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const label = b.getAttribute('aria-label') || '';
                    if (label === '设置' || label === 'Settings') {
                        b.click();
                        return true;
                    }
                }
                return false;
            })()
            """
        )
        if not settings_clicked:
            logger.warning("Could not find settings button for model selection")
            return

        await asyncio.sleep(0.5)

        escaped_model = model.replace("\\", "\\\\").replace("'", "\\'")
        await self._tab.evaluate(
            f"""
            (() => {{
                const items = document.querySelectorAll(
                    '[role="menuitem"], [role="option"], [role="radio"]'
                );
                for (const item of items) {{
                    const text = (item.textContent || '').toLowerCase();
                    if (text.includes('{escaped_model}'.toLowerCase())) {{
                        item.click();
                        return 'selected';
                    }}
                }}
                return 'not-found';
            }})()
            """
        )
        await asyncio.sleep(0.3)

    async def _snapshot_state(self) -> dict[str, Any]:
        """Snapshot current message count and canvas image count.

        Agent Mode DOM (validated 2026-06-28):
        - Chat panel: div.group/chat
        - Message list: child with overflow-y-auto
        - Each message: direct child of message list (div.flex.flex-col.gap-1.5.min-w-0)
        - User messages: contain a child with self-end class
        - Agent messages: contain a child with self-start class
        - Canvas images: img[src*="assets.grok.com"][src*="generated"]
        """
        result = await self._evaluate_json(
            """
            JSON.stringify((() => {
                // Find chat message list via input ancestor chain
                // The chat panel (group/chat) contains overflow-y-auto scroll area
                const scrollAreas = document.querySelectorAll('[class*="overflow-y-auto"]');
                let msgCount = 0;
                for (const area of scrollAreas) {
                    // The chat message list has children that are message bubbles
                    const parent = area.closest('[class*="group\\/chat"]')
                        || (area.className || '').toString().includes('flex-col')
                            && area.parentElement;
                    if (parent) {
                        msgCount = area.children.length;
                        break;
                    }
                }

                // Count canvas images (generated only, exclude profile pics)
                const imgs = document.querySelectorAll(
                    'img[src*="assets.grok.com"][src*="generated"]'
                );

                return {message_count: msgCount, image_count: imgs.length};
            })())
            """
        )
        if isinstance(result, dict):
            return result
        return {"message_count": 0, "image_count": 0}

    async def _wait_for_response(
        self,
        pre_msg_count: int,
        timeout: float,
        wait_for_images: bool,
        pre_image_count: int,
    ) -> dict[str, Any]:
        """Wait for Agent Mode response to stabilize.

        Detection logic:
        - Poll chat message list children count
        - When count > pre_msg_count, agent has started responding
        - Text stable: last assistant message text unchanged for 3s
        - Images stable: canvas image count unchanged for 5s
        """
        deadline = asyncio.get_event_loop().time() + timeout
        last_text = ""
        text_stable_since = asyncio.get_event_loop().time()
        last_image_count = pre_image_count
        images_stable_since = asyncio.get_event_loop().time()
        text_ready = False
        images_ready = not wait_for_images

        while asyncio.get_event_loop().time() < deadline:
            now = asyncio.get_event_loop().time()

            state = await self._evaluate_json(
                """
                JSON.stringify((() => {
                    // Find the chat message scroll area
                    const input = document.querySelector('[aria-label="Ask Grok anything"]');
                    let chatPanel = null;
                    if (input) {
                        let p = input;
                        for (let i = 0; i < 15; i++) {
                            p = p.parentElement;
                            if (!p) break;
                            if ((p.className || '').toString().includes('group/chat')) {
                                chatPanel = p;
                                break;
                            }
                        }
                    }

                    let msgCount = 0;
                    let lastAssistantText = '';
                    if (chatPanel) {
                        const msgList = chatPanel.querySelector('[class*="overflow-y-auto"]');
                        if (msgList) {
                            msgCount = msgList.children.length;
                            // Walk backwards to find last assistant message
                            for (let i = msgList.children.length - 1; i >= 0; i--) {
                                const child = msgList.children[i];
                                const hasSelfEnd = !!child.querySelector('[class*="self-end"]');
                                if (!hasSelfEnd) {
                                    // This is an assistant message
                                    lastAssistantText = (child.textContent || '').trim();
                                    break;
                                }
                            }
                        }
                    }

                    // Streaming indicator: stop button present
                    const streaming = !!document.querySelector(
                        'button[aria-label="停止"], button[aria-label="Stop"]'
                    );

                    // Canvas images
                    const imgs = document.querySelectorAll(
                        'img[src*="assets.grok.com"][src*="generated"]'
                    );

                    return {
                        msgCount: msgCount,
                        lastText: lastAssistantText,
                        streaming: streaming,
                        imgCount: imgs.length,
                    };
                })())
                """
            )

            if not isinstance(state, dict):
                await asyncio.sleep(0.5)
                continue

            current_text = state.get("lastText", "")
            current_msg_count = state.get("msgCount", 0)
            current_image_count = state.get("imgCount", 0)
            is_streaming = state.get("streaming", False)

            # Text stability: agent message appeared and text stopped changing
            if current_text != last_text:
                last_text = current_text
                text_stable_since = now
            elif (
                not is_streaming
                and current_text
                and now - text_stable_since >= 3.0
                and current_msg_count > pre_msg_count
            ):
                text_ready = True

            # Image stability
            if wait_for_images:
                if current_image_count != last_image_count:
                    last_image_count = current_image_count
                    images_stable_since = now
                elif not is_streaming and now - images_stable_since >= 5.0:
                    images_ready = True

            if text_ready and images_ready:
                break

            await asyncio.sleep(0.5)

        # Extract final response
        result = await self._evaluate_json(
            """
            JSON.stringify((() => {
                const input = document.querySelector('[aria-label="Ask Grok anything"]');
                let chatPanel = null;
                if (input) {
                    let p = input;
                    for (let i = 0; i < 15; i++) {
                        p = p.parentElement;
                        if (!p) break;
                        if ((p.className || '').toString().includes('group/chat')) {
                            chatPanel = p;
                            break;
                        }
                    }
                }

                let text = '';
                if (chatPanel) {
                    const msgList = chatPanel.querySelector('[class*="overflow-y-auto"]');
                    if (msgList) {
                        for (let i = msgList.children.length - 1; i >= 0; i--) {
                            const child = msgList.children[i];
                            const hasSelfEnd = !!child.querySelector('[class*="self-end"]');
                            if (!hasSelfEnd) {
                                text = (child.textContent || '').trim();
                                break;
                            }
                        }
                    }
                }

                // Canvas images (generated only)
                const imgEls = document.querySelectorAll(
                    'img[src*="assets.grok.com"][src*="generated"]'
                );
                const urls = [];
                for (const img of imgEls) {
                    if (img.src && !urls.includes(img.src)) {
                        urls.push(img.src);
                    }
                }

                return {text: text, image_urls: urls};
            })())
            """
        )

        if isinstance(result, dict):
            return result
        return {"text": "", "image_urls": []}

    # =========================================================================
    # Public API
    # =========================================================================

    async def send(self, params: dict) -> AgentResponse:
        """Send a message to Grok Agent Mode and wait for response.

        Use when: you want conversational image/video generation on Agent
        Mode's infinite canvas. Pass a message and optionally a session_url
        to resume a prior conversation.

        Args:
            params: Dict with keys:
                <SCHEMA_ARGS>

        Returns:
            AgentResponse with text, image_urls, and session_url for follow-up.

        Failure:
            GrokAPIError — chat input not found, submit failed, or timeout.
            GrokConfigError — invalid agent_model value.
        """
        cleaned = validate_params(params, AGENT_KEYS)

        message = cleaned.get("message")
        if not message:
            raise GrokConfigError("'message' is required for Agent Mode send()")

        session_url = cleaned.get("session_url")
        timeout = cleaned.get("timeout", 300)
        wait_for_images = cleaned.get("wait_for_images", True)
        agent_model = cleaned.get("agent_model")
        is_new = session_url is None

        # 1. Navigate
        target_url = session_url or AGENT_BASE_URL
        await self._tab.get(target_url)
        await asyncio.sleep(3)  # page hydration

        # Kill cookie banner (overlaps submit button coords)
        await self._kill_banner()

        # Wait for chat input to appear
        input_found = False
        for sel in _INPUT_SELECTORS:
            if await self._wait_for_selector(sel, timeout=15):
                input_found = True
                break

        if not input_found:
            raise GrokAPIError(
                f"Agent Mode chat input did not appear within 15s on {target_url}. "
                "Page may require login or the UI structure changed."
            )

        # Kill banner again (may re-mount after hydration)
        await self._kill_banner()

        # 2. For new conversations, ensure Agent mode radio is selected
        if is_new:
            await self._select_agent_mode()
            await asyncio.sleep(0.3)

        # 3. Snapshot pre-state
        pre_state = await self._snapshot_state()
        pre_msg_count = pre_state.get("message_count", 0)
        pre_image_count = pre_state.get("image_count", 0)

        # 4. Optional: set model
        if agent_model:
            await self._set_model(agent_model)

        # 5. Enter prompt
        await self._fill_input(message)
        await asyncio.sleep(0.5)

        # 6. Click submit
        await self._click_submit()

        # 7. Wait for response
        response_data = await self._wait_for_response(
            pre_msg_count=pre_msg_count,
            timeout=timeout,
            wait_for_images=wait_for_images,
            pre_image_count=pre_image_count,
        )

        # 8. Extract session URL from current page location
        current_url = await self._tab.evaluate("window.location.href")
        final_session_url = current_url if isinstance(current_url, str) else target_url

        # Filter image_urls to only new ones
        all_image_urls = response_data.get("image_urls", [])
        new_image_urls = (
            all_image_urls[pre_image_count:] if len(all_image_urls) > pre_image_count else []
        )

        return AgentResponse(
            session_url=final_session_url,
            text=response_data.get("text", ""),
            image_urls=new_image_urls,
            message_sent=message,
            is_new_conversation=is_new,
        )


# Splice schema into send() docstring at module load
from .schema import splice_schema_into_docstring  # noqa: E402

GrokAgentClient.send.__doc__ = splice_schema_into_docstring(
    GrokAgentClient.send.__doc__, AGENT_KEYS
)
