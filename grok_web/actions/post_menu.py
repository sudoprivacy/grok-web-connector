"""Post menu actions for Grok Imagine — the "..." dropdown on post pages.

Uses ai-dev-browser ax_tree (snapshot.find + ax.click_by_ref) for resilient
element discovery instead of brittle CSS selectors.
"""

import asyncio
import logging

from ..exceptions import GrokAPIError

logger = logging.getLogger(__name__)

# Menu button accessible names (Chinese + English) — used as a hint when
# Grok ships a labeled trigger. Since 2026-04 Grok also ships a nameless
# icon-only variant on some post types, in which case we fall back to the
# structural ``aria-haspopup="menu"`` selector (the Radix DropdownMenu
# Trigger invariant — durable across UI rewrites).
_MENU_BUTTON_NAMES = {"更多选项", "More options", "Options"}

_NAMELESS_TRIGGER_HINTED = False


async def open_post_menu(tab, *, delay: float = 1.0) -> bool:
    """Open the "..." post menu on the current page.

    Grok's menu is a Radix DropdownMenu — its Trigger listens to
    ``pointerdown`` events, not plain ``click``. Neither ``click_by_ref``
    (ax_tree) nor element.mouse_click() (CDP Input.dispatchMouseEvent
    without pointerType) produces a synthetic pointerdown that React
    sees, so the menu appears to click but never expands. We dispatch
    the pointer sequence via JS — that is what reliably opens the menu.

    Locator strategy (in priority order):
      1. ``button[aria-haspopup="menu"]`` whose ``aria-label`` matches
         one of :data:`_MENU_BUTTON_NAMES` (most specific).
      2. The lone ``button[aria-haspopup="menu"]`` on the page if
         exactly one exists (Radix DropdownMenu Trigger invariant —
         covers Grok's icon-only redesign that ships nameless).
      3. The first ``button[aria-haspopup="menu"]`` not nested inside
         a ``[role="menu"]`` container (excludes nested submenus when
         multiple triggers happen to be present).

    Assumes already navigated to a post page.

    Args:
        tab: browser Tab instance
        delay: UI delay multiplier

    Returns:
        True if menu was opened

    Raises:
        GrokAPIError: If no trigger candidate found / menu didn't open
    """
    import json as _json

    wanted_names_literal = _json.dumps(sorted(_MENU_BUTTON_NAMES))

    for attempt in range(3):
        # Locate the trigger and report which strategy matched so the
        # Python side can hint about UI drift exactly once.
        located = await tab.evaluate(
            r"""
            (() => {
                const wanted = new Set(__WANTED__);
                const all = Array.from(document.querySelectorAll('button[aria-haspopup="menu"]'))
                    .filter(b => {
                        // Visible only
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                if (all.length === 0) {
                    return {strategy: 'none', count: 0};
                }
                // Strategy 1: named match
                const named = all.find(b => wanted.has((b.getAttribute('aria-label') || '').trim()));
                if (named) {
                    return {strategy: 'named', count: all.length,
                            label: named.getAttribute('aria-label')};
                }
                // Strategy 2: lone trigger on the page
                if (all.length === 1) {
                    return {strategy: 'lone', count: 1, label: ''};
                }
                // Strategy 3: first trigger NOT nested in [role=menu]
                // (excludes any submenu triggers when multiple are present)
                const top = all.find(b => !b.closest('[role="menu"]'));
                if (top) {
                    return {strategy: 'top-level', count: all.length, label: ''};
                }
                return {strategy: 'ambiguous', count: all.length};
            })()
            """.replace("__WANTED__", wanted_names_literal)
        )

        if located and located.get("strategy") not in ("none", "ambiguous"):
            # One-time hint when Grok ships a nameless trigger so callers
            # know UI drift happened and the structural fallback is in use.
            global _NAMELESS_TRIGGER_HINTED
            if located["strategy"] in ("lone", "top-level") and not _NAMELESS_TRIGGER_HINTED:
                logger.info(
                    "[post_menu] Grok appears to ship an icon-only '...' "
                    "trigger (no aria-label); using structural "
                    "aria-haspopup='menu' fallback. Strategy=%s, count=%d. "
                    "If this is the only trigger candidate the connector "
                    "needed, no action is required — bumping connector "
                    "later will pick up an explicit aria-label if Grok "
                    "adds one.",
                    located["strategy"],
                    located["count"],
                )
                _NAMELESS_TRIGGER_HINTED = True

            # Pause any playing video so its overlay doesn't eat the click
            await tab.evaluate('document.querySelectorAll("video").forEach(v => v.pause())')
            await asyncio.sleep(0.2 * delay)

            # Fire a full pointer sequence on the same trigger we located.
            # Re-use the same selection logic on the JS side to avoid a
            # races where the DOM mutates between locate and click.
            fired = await tab.evaluate(
                r"""
                (() => {
                    const wanted = new Set(__WANTED__);
                    const all = Array.from(document.querySelectorAll('button[aria-haspopup="menu"]'))
                        .filter(b => {
                            const r = b.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        });
                    if (all.length === 0) return null;
                    let btn =
                        all.find(b => wanted.has((b.getAttribute('aria-label') || '').trim()))
                        || (all.length === 1 ? all[0] : null)
                        || all.find(b => !b.closest('[role="menu"]'));
                    if (!btn) return null;
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
                    return btn.getAttribute('aria-label') || '<nameless>';
                })()
                """.replace("__WANTED__", wanted_names_literal)
            )
            if fired:
                await asyncio.sleep(1 * delay)
                # Verify menu opened by checking that role=menuitem
                # nodes appeared (Radix renders these only when open).
                opened = await tab.evaluate(
                    """
                    document.querySelectorAll('[role="menuitem"]').length > 0
                    """
                )
                if opened:
                    return True
        if attempt < 2:
            await asyncio.sleep(2 * delay)

    # Diagnostic: dump visible buttons that COULD be menu triggers so
    # the next UI change is actionable without a live browser. Filter
    # out plain text-only tiles (e.g. template carousel) — keep only
    # buttons with aria-haspopup OR a non-empty aria-label, since those
    # are the candidates a future strategy might match on.
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
                    .filter(b => {
                        const aria = (b.getAttribute('aria-label') || '').trim();
                        const haspopup = (b.getAttribute('aria-haspopup') || '').trim();
                        return aria || haspopup;
                    })
                    .map(b => ({
                        aria: b.getAttribute('aria-label') || '',
                        haspopup: b.getAttribute('aria-haspopup') || '',
                        text: (b.innerText || '').trim().slice(0, 40),
                    }))
                    .slice(0, 30);
            })()
            """
        )
    except Exception:
        visible = None
    raise GrokAPIError(
        "Could not open '...' post menu (no aria-haspopup='menu' button "
        f"located, or Radix trigger isn't firing). Tried aria-labels: "
        f"{sorted(_MENU_BUTTON_NAMES)} + structural fallback. "
        f"Candidate buttons (filtered to aria-label or aria-haspopup): "
        f"{visible!r}. "
        "If this list contains a button that IS the '...' trigger, please "
        "open an issue with the dump so the locator strategy can be "
        "extended."
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
