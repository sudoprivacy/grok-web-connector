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
from .schema import (
    AGENT_KEYS,
    CANVAS_IMAGE_KEYS,
    CANVAS_TEXT_KEYS,
    CANVAS_UPLOAD_KEYS,
    CANVAS_VIDEO_KEYS,
    validate_params,
)

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

    # =========================================================================
    # Canvas tool helpers — validated 2026-06-29
    #
    # Canvas toolbar (bottom-right of react-flow area):
    #   x=813: cursor/select tool
    #   x=873: 生成图片 (⌘I) — opens prompt input on canvas
    #   x=917: 生成视频 (⌘U) — opens prompt input on canvas
    #   x=961: 文本 (⌘E) — opens text input on canvas
    #   x=1005: 上传图片 — opens file picker
    #
    # Canvas prompt input UI (after clicking toolbar button):
    #   - Input: [aria-label="Ask Grok anything"] at y < 400 (canvas area)
    #   - Submit: [aria-label="发送"] (NOT "提交" — canvas uses 发送)
    #   - Cancel: [aria-label="取消"]
    #   - Image count: [aria-label="图像数量"]
    #   - Quality: buttons "速度"/"质量"
    #   - Aspect ratio: [aria-label="宽高比"]
    # =========================================================================

    async def _ensure_conversation(self, session_url: str | None) -> str:
        """Ensure we're in an active Agent Mode conversation.

        Canvas toolbar only appears inside an active conversation. If
        session_url differs from the current page, navigates to it.
        If no session_url and no active conversation, bootstraps one
        via send(). Returns the effective session URL.
        """
        current_url = await self._tab.evaluate("window.location.href")
        current_url = current_url if isinstance(current_url, str) else ""

        if session_url and session_url != current_url:
            await self._tab.get(session_url)
            await asyncio.sleep(3)
            await self._kill_banner()
            for sel in _INPUT_SELECTORS:
                if await self._wait_for_selector(sel, timeout=15):
                    break
            await self._kill_banner()
            return session_url

        # Check if we're already in an active conversation
        # (chat panel visible with messages)
        has_chat = await self._evaluate_json(
            """
            JSON.stringify((() => {
                const input = document.querySelector('[aria-label="Ask Grok anything"]');
                if (!input || !input.offsetHeight) return false;
                let p = input;
                for (let i = 0; i < 15; i++) {
                    p = p.parentElement;
                    if (!p) break;
                    if ((p.className || '').toString().includes('group/chat')) return true;
                }
                return false;
            })())
            """
        )

        if has_chat:
            await self._kill_banner()
            return current_url

        # No active conversation — bootstrap one via send()
        await self.send(
            {
                "message": "Start a new canvas session",
                "wait_for_images": False,
                "timeout": 30,
            }
        )
        # Don't navigate — stay on the current page with the active conversation
        await asyncio.sleep(1)
        final_url = await self._tab.evaluate("window.location.href")
        return final_url if isinstance(final_url, str) else current_url

    async def _click_canvas_toolbar(self, tooltip_text: str) -> bool:
        """Click a canvas toolbar button by hovering to match tooltip.

        The canvas toolbar buttons have no aria-label — they're icon-only.
        We identify them by hovering and reading the Radix tooltip text.

        Args:
            tooltip_text: Expected tooltip text (e.g. '生成图片', '生成视频', '文本')
        """
        from ai_dev_browser import cdp

        # Find icon-only toolbar buttons in the bottom bar.
        # Canvas toolbar buttons: bottom area, icon-only (SVG child, no text),
        # typically have class containing "rounded-[11px]" or similar.
        # We look for SVG-only buttons with no meaningful text/aria-label.
        positions = await self._evaluate_json(
            """
            JSON.stringify((() => {
                const buttons = document.querySelectorAll('button');
                const toolbar = [];
                for (const b of buttons) {
                    const r = b.getBoundingClientRect();
                    const ariaLabel = b.getAttribute('aria-label') || '';
                    const text = (b.textContent || '').trim();
                    const hasSvg = !!b.querySelector('svg');
                    // Canvas toolbar: bottom area, has SVG, small-ish width,
                    // no meaningful aria-label (exclude known non-toolbar buttons)
                    if (r.y > 830 && b.offsetHeight > 0 && hasSvg
                        && r.width >= 28 && r.width <= 80
                        && !ariaLabel.includes('听写') && !ariaLabel.includes('提交')
                        && !ariaLabel.includes('Submit') && !ariaLabel.includes('发送')
                        && !ariaLabel.includes('缩小') && !ariaLabel.includes('放大')
                        && !ariaLabel.includes('更多')
                        && !text.includes('%')
                        && !text.includes('报告')
                        && ariaLabel !== '添加图片'
                        ) {
                        toolbar.push({
                            x: Math.round(r.x + r.width/2),
                            y: Math.round(r.y + r.height/2),
                        });
                    }
                }
                return toolbar;
            })())
            """
        )

        if not isinstance(positions, list) or not positions:
            logger.warning("No canvas toolbar buttons found")
            return False

        for pos in positions:
            # Hover to reveal tooltip
            await self._tab.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mouseMoved",
                    x=pos["x"],
                    y=pos["y"],
                )
            )
            await asyncio.sleep(1.0)

            # Read tooltip
            tip = await self._evaluate_json(
                """
                JSON.stringify((() => {
                    const tips = document.querySelectorAll(
                        '[data-state="delayed-open"], [data-state="instant-open"], '
                        + '[role="tooltip"], [data-radix-portal]'
                    );
                    for (const t of tips) {
                        const text = (t.textContent || '').trim();
                        if (text && t.offsetHeight > 0) return text;
                    }
                    return '';
                })())
                """
            )

            # Move away to close tooltip
            await self._tab.send(cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=0, y=0))
            await asyncio.sleep(0.2)

            if isinstance(tip, str) and tooltip_text in tip:
                # Click this button
                await self._tab.evaluate(
                    f"""
                    (() => {{
                        const buttons = document.querySelectorAll('button');
                        for (const b of buttons) {{
                            const r = b.getBoundingClientRect();
                            if (Math.round(r.x + r.width/2) === {pos["x"]}
                                && Math.round(r.y + r.height/2) === {pos["y"]}) {{
                                b.click();
                                return 'clicked';
                            }}
                        }}
                    }})()
                    """
                )
                await asyncio.sleep(0.5)
                return True

        logger.warning(f"Canvas toolbar button with tooltip '{tooltip_text}' not found")
        return False

    async def _fill_canvas_input(self, text: str) -> None:
        """Fill the canvas-area prompt input (not the chat input).

        The canvas input appears after clicking a toolbar button (生成图片 etc.)
        at y < 400. The chat input is at y > 800.
        """
        escaped = text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
        fill_result = await self._tab.evaluate(
            f"""
            (() => {{
                const inputs = document.querySelectorAll('[aria-label="Ask Grok anything"]');
                for (const inp of inputs) {{
                    const r = inp.getBoundingClientRect();
                    if (r.y < 400 && inp.offsetHeight > 0) {{
                        inp.focus();
                        document.execCommand('selectAll');
                        document.execCommand('delete');
                        document.execCommand('insertText', false, `{escaped}`);
                        return 'ok';
                    }}
                }}
                return 'no-canvas-input';
            }})()
            """
        )
        if fill_result == "no-canvas-input":
            raise GrokAPIError(
                "Canvas prompt input not found. Did the toolbar button click succeed? "
                "Expected an input at y < 400 with aria-label='Ask Grok anything'."
            )

    async def _click_canvas_submit(self) -> None:
        """Click the canvas prompt submit button (aria-label="发送").

        The canvas prompt uses "发送" (Send), distinct from the chat's "提交" (Submit).
        The canvas submit is near the canvas input (y < 400).
        """
        clicked = await self._tab.evaluate(
            """
            (() => {
                // Find 发送 button near the canvas input (y < 400)
                const buttons = document.querySelectorAll('button[aria-label="发送"]');
                for (const b of buttons) {
                    const r = b.getBoundingClientRect();
                    if (r.y < 400 && !b.disabled && b.offsetHeight > 0) {
                        b.click();
                        return true;
                    }
                }
                // Fallback: any non-disabled 发送 button
                for (const b of buttons) {
                    if (!b.disabled && b.offsetHeight > 0) {
                        b.click();
                        return true;
                    }
                }
                return false;
            })()
            """
        )
        if not clicked:
            raise GrokAPIError(
                "Canvas submit button (aria-label='发送') not found or disabled. "
                "Make sure the canvas prompt input has text."
            )

    async def _count_canvas_nodes(self) -> dict[str, Any]:
        """Count react-flow nodes and their image URLs (canvas only)."""
        result = await self._evaluate_json(
            """
            JSON.stringify((() => {
                const nodes = document.querySelectorAll('.react-flow__node');
                const urls = [];
                for (const n of nodes) {
                    const imgs = n.querySelectorAll('img');
                    for (const img of imgs) {
                        if (img.src && img.src.includes('assets.grok.com')
                            && !urls.includes(img.src)) {
                            urls.push(img.src);
                        }
                    }
                }
                return {node_count: nodes.length, urls: urls};
            })())
            """
        )
        if isinstance(result, dict):
            return result
        return {"node_count": 0, "urls": []}

    async def _wait_for_canvas_images(
        self,
        pre_urls: list[str],
        timeout: float,
    ) -> list[str]:
        """Wait for new images on the canvas (react-flow nodes only)."""
        deadline = asyncio.get_event_loop().time() + timeout
        pre_set = set(pre_urls)
        last_new_count = 0
        stable_since = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() < deadline:
            state = await self._count_canvas_nodes()
            current_urls = state.get("urls", [])
            new_urls = [u for u in current_urls if u not in pre_set]

            if len(new_urls) != last_new_count:
                last_new_count = len(new_urls)
                stable_since = asyncio.get_event_loop().time()
            elif new_urls and asyncio.get_event_loop().time() - stable_since >= 5.0:
                return new_urls

            await asyncio.sleep(1)

        # Timeout — return whatever new images we found
        state = await self._count_canvas_nodes()
        return [u for u in state.get("urls", []) if u not in pre_set]

    async def _set_canvas_option(self, aria_label: str, value: str) -> bool:
        """Click a canvas prompt option button and select a value.

        Used for aspect_ratio (宽高比) and image_count (图像数量).
        """
        # Click the option button to open dropdown
        clicked = await self._tab.evaluate(
            f"""
            (() => {{
                const btn = document.querySelector('button[aria-label="{aria_label}"]');
                if (btn && btn.offsetHeight > 0) {{
                    btn.click();
                    return true;
                }}
                return false;
            }})()
            """
        )
        if not clicked:
            return False

        await asyncio.sleep(0.5)

        # Click the value option
        escaped_value = value.replace("\\", "\\\\").replace("'", "\\'")
        selected = await self._tab.evaluate(
            f"""
            (() => {{
                const items = document.querySelectorAll(
                    '[role="menuitem"], [role="option"], [role="radio"], button'
                );
                for (const item of items) {{
                    const text = (item.textContent || '').trim();
                    if (text === '{escaped_value}' && item.offsetHeight > 0) {{
                        item.click();
                        return true;
                    }}
                }}
                return false;
            }})()
            """
        )
        await asyncio.sleep(0.3)
        return bool(selected)

    # =========================================================================
    # Phase 2: Canvas tool public methods
    # =========================================================================

    async def canvas_generate_image(self, params: dict) -> AgentResponse:
        """Generate image(s) directly on the canvas via the 生成图片 toolbar.

        Use when: you want to place generated images directly on the
        infinite canvas without going through the chat dialog. This
        clicks the canvas toolbar's image button, fills the prompt,
        and waits for generation.

        Args:
            params: Dict with keys:
                <SCHEMA_ARGS>

        Returns:
            AgentResponse with new image_urls on the canvas.

        Failure:
            GrokAPIError — toolbar button not found, canvas input missing, or timeout.
            GrokConfigError — missing required prompt.
        """
        cleaned = validate_params(params, CANVAS_IMAGE_KEYS)

        prompt = cleaned.get("prompt")
        if not prompt:
            raise GrokConfigError("'prompt' is required for canvas_generate_image()")

        session_url = cleaned.get("session_url")
        timeout = cleaned.get("timeout", 300)
        aspect_ratio = cleaned.get("aspect_ratio")
        quality = cleaned.get("quality")
        image_count = cleaned.get("image_count")

        # 1. Navigate
        target_url = await self._ensure_conversation(session_url)

        # 2. Snapshot canvas state (react-flow nodes only)
        pre_canvas = await self._count_canvas_nodes()
        pre_urls = pre_canvas.get("urls", [])

        # 3. Click 生成图片 toolbar button
        if not await self._click_canvas_toolbar("生成图片"):
            raise GrokAPIError(
                "Could not find 生成图片 (Generate Image) canvas toolbar button. "
                "The canvas toolbar may not be visible."
            )
        await asyncio.sleep(0.5)

        # 4. Set options before filling prompt
        if aspect_ratio:
            await self._set_canvas_option("宽高比", aspect_ratio)
        if quality:
            quality_label = "质量" if quality == "quality" else "速度"
            await self._tab.evaluate(
                f"""
                (() => {{
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {{
                        if ((b.textContent || '').trim() === '{quality_label}'
                            && b.offsetHeight > 0) {{
                            b.click();
                            return;
                        }}
                    }}
                }})()
                """
            )
            await asyncio.sleep(0.3)
        if image_count is not None:
            await self._set_canvas_option("图像数量", str(image_count))

        # 5. Fill canvas prompt
        await self._fill_canvas_input(prompt)
        await asyncio.sleep(0.5)

        # 6. Click canvas submit (发送)
        await self._click_canvas_submit()

        # 7. Wait for images
        new_urls = await self._wait_for_canvas_images(
            pre_urls=pre_urls,
            timeout=timeout,
        )

        # 8. Get session URL
        current_url = await self._tab.evaluate("window.location.href")
        final_session_url = current_url if isinstance(current_url, str) else target_url

        return AgentResponse(
            session_url=final_session_url,
            text="",
            image_urls=new_urls,
            message_sent=prompt,
            is_new_conversation=session_url is None,
        )

    async def canvas_generate_video(self, params: dict) -> AgentResponse:
        """Generate video on the canvas via the 生成视频 toolbar.

        Use when: you want to create a video directly on the canvas.

        Args:
            params: Dict with keys:
                <SCHEMA_ARGS>

        Returns:
            AgentResponse — video generation triggers but tracking is limited
            since videos appear as canvas nodes, not image URLs.

        Failure:
            GrokAPIError — toolbar button not found or timeout.
            GrokConfigError — missing required prompt.
        """
        cleaned = validate_params(params, CANVAS_VIDEO_KEYS)

        prompt = cleaned.get("prompt")
        if not prompt:
            raise GrokConfigError("'prompt' is required for canvas_generate_video()")

        session_url = cleaned.get("session_url")

        # 1. Navigate
        target_url = await self._ensure_conversation(session_url)

        # 2. Click 生成视频 toolbar button
        if not await self._click_canvas_toolbar("生成视频"):
            raise GrokAPIError("Could not find 生成视频 (Generate Video) canvas toolbar button.")
        await asyncio.sleep(0.5)

        # 3. Fill canvas prompt
        await self._fill_canvas_input(prompt)
        await asyncio.sleep(0.5)

        # 4. Click canvas submit (发送)
        await self._click_canvas_submit()

        # 5. Wait briefly — video generation is async and tracked differently
        await asyncio.sleep(5)

        # 6. Get session URL
        current_url = await self._tab.evaluate("window.location.href")
        final_session_url = current_url if isinstance(current_url, str) else target_url

        return AgentResponse(
            session_url=final_session_url,
            text="",
            image_urls=[],
            message_sent=prompt,
            is_new_conversation=session_url is None,
        )

    async def canvas_add_text(self, params: dict) -> AgentResponse:
        """Place text on the canvas via the 文本 toolbar.

        Use when: you want to add text content to the infinite canvas.

        Args:
            params: Dict with keys:
                <SCHEMA_ARGS>

        Returns:
            AgentResponse with session_url.

        Failure:
            GrokAPIError — toolbar button not found or text input missing.
            GrokConfigError — missing required canvas_text.
        """
        cleaned = validate_params(params, CANVAS_TEXT_KEYS)

        text = cleaned.get("canvas_text")
        if not text:
            raise GrokConfigError("'canvas_text' is required for canvas_add_text()")

        session_url = cleaned.get("session_url")

        # 1. Navigate
        target_url = await self._ensure_conversation(session_url)

        # 2. Click 文本 toolbar button
        if not await self._click_canvas_toolbar("文本"):
            raise GrokAPIError("Could not find 文本 (Text) canvas toolbar button.")
        await asyncio.sleep(0.5)

        # 3. The text tool opens a text input on canvas — type into it
        escaped = text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
        await self._tab.evaluate(
            f"""
            (() => {{
                // After clicking 文本, a contenteditable text node appears on canvas
                const editables = document.querySelectorAll('[contenteditable="true"]');
                for (const ed of editables) {{
                    const r = ed.getBoundingClientRect();
                    // Canvas text input is in the react-flow area (not the chat panel)
                    if (r.y < 800 && ed.offsetHeight > 0) {{
                        ed.focus();
                        document.execCommand('insertText', false, `{escaped}`);
                        return 'ok';
                    }}
                }}
                return 'no-text-input';
            }})()
            """
        )
        await asyncio.sleep(0.5)

        # Click elsewhere on canvas to deselect
        await self._tab.evaluate(
            """
            (() => {
                const rf = document.querySelector('.react-flow__pane');
                if (rf) rf.click();
            })()
            """
        )

        current_url = await self._tab.evaluate("window.location.href")
        final_session_url = current_url if isinstance(current_url, str) else target_url

        return AgentResponse(
            session_url=final_session_url,
            text="",
            image_urls=[],
            message_sent=text,
            is_new_conversation=session_url is None,
        )

    async def canvas_upload_image(self, params: dict) -> AgentResponse:
        """Upload a local image to the canvas via the 上传图片 toolbar.

        Use when: you want to place an existing image on the canvas.

        Args:
            params: Dict with keys:
                <SCHEMA_ARGS>

        Returns:
            AgentResponse with session_url.

        Failure:
            GrokAPIError — toolbar button not found or upload failed.
            GrokConfigError — missing required file_path or file not found.
        """
        cleaned = validate_params(params, CANVAS_UPLOAD_KEYS)

        file_path = cleaned.get("file_path")
        if not file_path:
            raise GrokConfigError("'file_path' is required for canvas_upload_image()")

        from pathlib import Path as _Path

        path = _Path(file_path)
        if not path.exists():
            raise GrokConfigError(f"File not found: {file_path}")

        session_url = cleaned.get("session_url")

        # 1. Navigate
        target_url = await self._ensure_conversation(session_url)

        # 2. Click 上传图片 toolbar button
        if not await self._click_canvas_toolbar("上传图片"):
            raise GrokAPIError("Could not find 上传图片 (Upload Image) canvas toolbar button.")
        await asyncio.sleep(0.5)

        # 3. Set file on the hidden file input
        from ai_dev_browser import cdp

        # Find the file input element
        abs_path = str(path.resolve())
        file_set = await self._tab.evaluate(
            """
            JSON.stringify((() => {
                const inputs = document.querySelectorAll('input[type="file"]');
                for (const inp of inputs) {
                    return {found: true, id: inp.id, name: inp.name};
                }
                return {found: false};
            })())
            """
        )

        if isinstance(file_set, str):
            file_set = json_mod.loads(file_set)

        if not file_set or not file_set.get("found"):
            raise GrokAPIError("File input element not found after clicking 上传图片")

        # Use CDP DOM.setFileInputFiles to set the file
        # First get the file input node
        doc = await self._tab.send(cdp.dom.get_document())
        file_input_node = await self._tab.send(
            cdp.dom.query_selector(doc.node_id, 'input[type="file"]')
        )
        if file_input_node:
            await self._tab.send(
                cdp.dom.set_file_input_files(
                    files=[abs_path],
                    node_id=file_input_node,
                )
            )
            await asyncio.sleep(2)

        current_url = await self._tab.evaluate("window.location.href")
        final_session_url = current_url if isinstance(current_url, str) else target_url

        return AgentResponse(
            session_url=final_session_url,
            text="",
            image_urls=[],
            message_sent=str(file_path),
            is_new_conversation=session_url is None,
        )


# Splice schema into docstrings at module load
from .schema import splice_schema_into_docstring  # noqa: E402

GrokAgentClient.send.__doc__ = splice_schema_into_docstring(
    GrokAgentClient.send.__doc__, AGENT_KEYS
)
GrokAgentClient.canvas_generate_image.__doc__ = splice_schema_into_docstring(
    GrokAgentClient.canvas_generate_image.__doc__, CANVAS_IMAGE_KEYS
)
GrokAgentClient.canvas_generate_video.__doc__ = splice_schema_into_docstring(
    GrokAgentClient.canvas_generate_video.__doc__, CANVAS_VIDEO_KEYS
)
GrokAgentClient.canvas_add_text.__doc__ = splice_schema_into_docstring(
    GrokAgentClient.canvas_add_text.__doc__, CANVAS_TEXT_KEYS
)
GrokAgentClient.canvas_upload_image.__doc__ = splice_schema_into_docstring(
    GrokAgentClient.canvas_upload_image.__doc__, CANVAS_UPLOAD_KEYS
)
