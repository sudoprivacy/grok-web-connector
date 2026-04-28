"""Post menu actions for Grok Imagine — the "..." dropdown on post pages.

Uses ai-dev-browser ax_tree (snapshot.find + ax.click_by_ref) for resilient
element discovery instead of brittle CSS selectors.
"""

import asyncio
import logging

from ..exceptions import GrokAPIError

logger = logging.getLogger(__name__)

# Menu button accessible names (Chinese + English)
_MENU_BUTTON_NAMES = {"更多选项", "More options", "Options"}


async def open_post_menu(tab, *, delay: float = 1.0) -> bool:
    """Open the "..." post menu on the current page.

    Grok's menu is a Radix DropdownMenu — its Trigger listens to
    ``pointerdown`` events, not plain ``click``. Neither ``click_by_ref``
    (ax_tree) nor element.mouse_click() (CDP Input.dispatchMouseEvent
    without pointerType) produces a synthetic pointerdown that React
    sees, so the menu appears to click but never expands. We dispatch
    the pointer sequence via JS — that is what reliably opens the menu.

    Assumes already navigated to a post page.

    Args:
        tab: browser Tab instance
        delay: UI delay multiplier

    Returns:
        True if menu was opened

    Raises:
        GrokAPIError: If menu button not found after retries
    """
    for attempt in range(3):
        # Confirm the button is present before trying to click it.
        present = await tab.evaluate(
            """
            (() => {
                const sels = [
                    'button[aria-label="更多选项"][aria-haspopup="menu"]',
                    'button[aria-label="More options"][aria-haspopup="menu"]',
                    'button[aria-label="Options"][aria-haspopup="menu"]',
                    'button[aria-label="更多选项"]',
                    'button[aria-label="More options"]',
                    'button[aria-label="Options"]',
                ];
                for (const sel of sels) {
                    const btn = document.querySelector(sel);
                    if (btn) return true;
                }
                return false;
            })()
            """
        )
        if present:
            # Pause any playing video so its overlay doesn't eat the click
            await tab.evaluate('document.querySelectorAll("video").forEach(v => v.pause())')
            await asyncio.sleep(0.2 * delay)
            # Fire a full pointer sequence — Radix opens on pointerdown
            fired = await tab.evaluate(
                """
                (() => {
                    const sels = [
                        'button[aria-label="更多选项"]',
                        'button[aria-label="More options"]',
                        'button[aria-label="Options"]',
                    ];
                    for (const sel of sels) {
                        const btn = document.querySelector(sel);
                        if (!btn) continue;
                        const r = btn.getBoundingClientRect();
                        const x = r.x + r.width/2, y = r.y + r.height/2;
                        const o = {bubbles: true, cancelable: true,
                                   clientX: x, clientY: y,
                                   pointerType: 'mouse', button: 0,
                                   pointerId: 1, isPrimary: true};
                        btn.dispatchEvent(new PointerEvent('pointerover', o));
                        btn.dispatchEvent(new PointerEvent('pointerenter', o));
                        btn.dispatchEvent(new PointerEvent('pointermove', o));
                        btn.dispatchEvent(new PointerEvent('pointerdown', o));
                        btn.dispatchEvent(new MouseEvent('mousedown', o));
                        btn.dispatchEvent(new PointerEvent('pointerup', o));
                        btn.dispatchEvent(new MouseEvent('mouseup', o));
                        btn.dispatchEvent(new MouseEvent('click', o));
                        return sel;
                    }
                    return null;
                })()
                """
            )
            if fired:
                await asyncio.sleep(1 * delay)
                # Verify menu opened by checking data-state / menuitems
                opened = await tab.evaluate(
                    """
                    document.querySelectorAll('[role="menuitem"]').length > 0
                    """
                )
                if opened:
                    return True
        if attempt < 2:
            await asyncio.sleep(2 * delay)

    # Diagnostic: dump visible button labels so the next failure is
    # actionable without a live browser. Grok occasionally renames the
    # aria-label or restructures the menu trigger — this list tells us
    # exactly what to add to _MENU_BUTTON_NAMES.
    try:
        visible = await tab.evaluate(
            r"""
            (() => {
                const btns = Array.from(document.querySelectorAll('button'));
                return btns
                    .filter(b => {
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    })
                    .map(b => ({
                        aria: b.getAttribute('aria-label') || '',
                        haspopup: b.getAttribute('aria-haspopup') || '',
                        text: (b.innerText || '').trim().slice(0, 40),
                    }))
                    .filter(d => d.aria || d.haspopup || d.text)
                    .slice(0, 30);
            })()
            """
        )
    except Exception:
        visible = None
    raise GrokAPIError(
        "Could not open '...' post menu (button may be missing or Radix "
        "trigger isn't firing). Tried aria-labels: "
        f"{sorted(_MENU_BUTTON_NAMES)}. "
        f"Visible buttons on page: {visible!r}. "
        "If the page actually has a '...' menu but a different aria-label, "
        "add it to grok_web/actions/post_menu.py::_MENU_BUTTON_NAMES."
    )


async def click_menu_item(tab, *text_options: str, delay: float = 1.0) -> bool:
    """Click a menu item by matching its accessible name.

    Radix menu items — like their Trigger — respond to ``pointerdown``,
    not to ``click``. ``click_by_ref`` (ax_tree) and ``element.mouse_click``
    (CDP Input.dispatchMouseEvent without pointerType) both appear to
    click successfully but do NOT actually fire Radix's handler, leaving
    the menu open. We dispatch a full pointer sequence via JS; that is
    what reliably activates the item.

    Args:
        tab: browser Tab instance
        *text_options: Text strings to match (e.g., "扩展", "Extend video")
        delay: UI delay multiplier

    Returns:
        True if item was clicked

    Raises:
        GrokAPIError: If no matching menu item found
    """
    import json as _json

    wanted_literal = _json.dumps(list(text_options))

    for attempt in range(3):
        # Match on both innerText (text-only menuitems like "扩展")
        # and aria-label (icon-only menuitems like 赞/踩 which have
        # empty innerText and carry the label on aria-label).
        js = r"""
            (() => {
                const wanted = new Set(__WANTED__);
                const items = Array.from(document.querySelectorAll('[role="menuitem"]'));
                for (const mi of items) {
                    const t = (mi.innerText || '').trim();
                    const al = (mi.getAttribute('aria-label') || '').trim();
                    if (!wanted.has(t) && !wanted.has(al)) continue;
                    const r = mi.getBoundingClientRect();
                    const x = r.x + r.width/2, y = r.y + r.height/2;
                    const o = {bubbles: true, cancelable: true,
                               clientX: x, clientY: y,
                               pointerType: 'mouse', button: 0,
                               pointerId: 1, isPrimary: true};
                    mi.dispatchEvent(new PointerEvent('pointerover', o));
                    mi.dispatchEvent(new PointerEvent('pointerenter', o));
                    mi.dispatchEvent(new PointerEvent('pointermove', o));
                    mi.dispatchEvent(new PointerEvent('pointerdown', o));
                    mi.dispatchEvent(new MouseEvent('mousedown', o));
                    mi.dispatchEvent(new PointerEvent('pointerup', o));
                    mi.dispatchEvent(new MouseEvent('mouseup', o));
                    mi.dispatchEvent(new MouseEvent('click', o));
                    return t || al;
                }
                return null;
            })()
        """.replace("__WANTED__", wanted_literal)
        fired = await tab.evaluate(js)
        if fired:
            await asyncio.sleep(0.5 * delay)
            return True
        if attempt < 2:
            await asyncio.sleep(1 * delay)

    raise GrokAPIError(f"Could not find menu item: {text_options}")


async def get_menu_items(tab) -> list[dict]:
    """Get all visible menu items via ax_tree.

    Useful for debugging — see what items are in the currently open menu.

    Returns:
        List of dicts with 'name', 'role', 'ref' for each menuitem
    """
    from ai_dev_browser.core.snapshot import page_discover as page_find

    result = await page_find(tab, interactable_only=True)
    return [
        {"name": el.get("name", ""), "role": el.get("role"), "ref": el.get("ref")}
        for el in result.get("elements", [])
        if el.get("role") == "menuitem"
    ]
