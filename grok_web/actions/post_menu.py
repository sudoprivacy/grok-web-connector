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


async def open_post_menu(tab, *, delay: float = 1.0, prefer_media: str | None = None) -> bool:
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
      3. **Spatial proximity to preferred media** — when multiple
         triggers exist (chain-root posts render BOTH the image card
         and the tail video card, each with its own "..." menu), the
         trigger whose center is closest to the largest visible
         ``<img>`` (``prefer_media="image"``) or ``<video>``
         (``prefer_media="video"``) or any media (``None``) wins.
         Caller passes the hint based on what menu items it expects
         to find — image-context vs video-context.
      4. The first ``button[aria-haspopup="menu"]`` not nested inside
         a ``[role="menu"]`` container (last resort when no media
         element to anchor against).

    Assumes already navigated to a post page.

    Args:
        tab: browser Tab instance
        delay: UI delay multiplier
        prefer_media: ``"image"`` or ``"video"`` to disambiguate when
            multiple triggers exist on chain-root posts. ``None`` =
            any visible media (largest wins).

    Returns:
        True if menu was opened

    Raises:
        GrokAPIError: If no trigger candidate found / menu didn't open
    """
    import json as _json

    wanted_names_literal = _json.dumps(sorted(_MENU_BUTTON_NAMES))
    if prefer_media == "image":
        media_selector = "img"
    elif prefer_media == "video":
        media_selector = "video"
    else:
        media_selector = "img, video"
    media_selector_literal = _json.dumps(media_selector)

    # Locate + click in a single JS call to avoid the DOM-mutates-between-
    # locate-and-click race. Strategy chosen on the JS side; reported back
    # so the Python side can log UI-drift hints exactly once.
    select_and_click_js = r"""
        (() => {
            const wanted = new Set(__WANTED__);
            const mediaSelector = __MEDIA_SELECTOR__;
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const all = Array.from(
                document.querySelectorAll('button[aria-haspopup="menu"]')
            ).filter(visible);

            if (all.length === 0) {
                return JSON.stringify({strategy: 'none', count: 0});
            }

            let btn = null;
            let strategy = '';
            let extra = {};

            // Strategy 1: named match
            const named = all.find(
                b => wanted.has((b.getAttribute('aria-label') || '').trim())
            );
            if (named) {
                btn = named;
                strategy = 'named';
                extra.label = named.getAttribute('aria-label');
            } else if (all.length === 1) {
                // Strategy 2: lone trigger
                btn = all[0];
                strategy = 'lone';
            } else {
                // Strategy 3: spatial proximity to largest preferred media.
                // Chain-root posts render image card AND video card, each
                // with its own '...' trigger; pick the one closest to the
                // media element the caller cares about.
                const media = Array.from(document.querySelectorAll(mediaSelector))
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 200 && r.height > 200;
                    });
                if (media.length > 0) {
                    media.sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (br.width * br.height) - (ar.width * ar.height);
                    });
                    const target = media[0];
                    const tr = target.getBoundingClientRect();
                    const tx = tr.x + tr.width / 2;
                    const ty = tr.y + tr.height / 2;
                    const ranked = all.map(b => {
                        const r = b.getBoundingClientRect();
                        const dx = (r.x + r.width / 2) - tx;
                        const dy = (r.y + r.height / 2) - ty;
                        return {btn: b, dist: Math.sqrt(dx * dx + dy * dy)};
                    }).sort((a, b) => a.dist - b.dist);
                    btn = ranked[0].btn;
                    strategy = 'spatial';
                    extra.target_tag = target.tagName;
                    extra.distance = Math.round(ranked[0].dist);
                } else {
                    // Strategy 4: first non-nested top-level trigger.
                    btn = all.find(b => !b.closest('[role="menu"]'));
                    if (btn) {
                        strategy = 'top-level';
                    } else {
                        return JSON.stringify({
                            strategy: 'ambiguous', count: all.length
                        });
                    }
                }
            }

            // Click the chosen trigger via full pointer sequence.
            const r = btn.getBoundingClientRect();
            const x = r.x + r.width / 2;
            const y = r.y + r.height / 2;
            const o = {
                bubbles: true, cancelable: true,
                clientX: x, clientY: y,
                pointerType: 'mouse', button: 0,
                pointerId: 1, isPrimary: true,
            };
            btn.dispatchEvent(new PointerEvent('pointerover', o));
            btn.dispatchEvent(new PointerEvent('pointerenter', o));
            btn.dispatchEvent(new PointerEvent('pointermove', o));
            btn.dispatchEvent(new PointerEvent('pointerdown', o));
            btn.dispatchEvent(new MouseEvent('mousedown', o));
            btn.dispatchEvent(new PointerEvent('pointerup', o));
            btn.dispatchEvent(new MouseEvent('mouseup', o));
            btn.dispatchEvent(new MouseEvent('click', o));

            return JSON.stringify({
                strategy,
                count: all.length,
                label: btn.getAttribute('aria-label') || '<nameless>',
                ...extra,
            });
        })()
        """.replace("__WANTED__", wanted_names_literal).replace(
        "__MEDIA_SELECTOR__", media_selector_literal
    )

    for attempt in range(3):
        # Pause any playing video so its overlay doesn't eat clicks anywhere
        # on the page (including the trigger we're about to click).
        await tab.evaluate('document.querySelectorAll("video").forEach(v => v.pause())')
        await asyncio.sleep(0.2 * delay)

        result_raw = await tab.evaluate(select_and_click_js)
        try:
            result = _json.loads(result_raw) if isinstance(result_raw, str) else None
        except (TypeError, ValueError):
            result = None

        if result and result.get("strategy") not in (None, "none", "ambiguous"):
            # One-time hint when we land on a non-named strategy so UI
            # drift is observable. Spatial path is the chain-root case;
            # lone/top-level are the icon-only-trigger redesign.
            global _NAMELESS_TRIGGER_HINTED
            if (
                result["strategy"] in ("lone", "top-level", "spatial")
                and not _NAMELESS_TRIGGER_HINTED
            ):
                logger.info(
                    "[post_menu] using structural aria-haspopup='menu' "
                    "fallback. Strategy=%s, count=%d, prefer_media=%r. "
                    "If this matches the menu items the caller expected, "
                    "no action needed; if not, file an issue with the "
                    "post UUID.",
                    result["strategy"],
                    result["count"],
                    prefer_media,
                )
                _NAMELESS_TRIGGER_HINTED = True

            await asyncio.sleep(1 * delay)
            # Verify menu actually opened (role=menuitem nodes appear).
            opened = await tab.evaluate(
                """document.querySelectorAll('[role="menuitem"]').length > 0"""
            )
            if opened:
                return True

        if attempt < 2:
            await asyncio.sleep(2 * delay)

    # Diagnostic: dump (a) candidate buttons and (b) for each
    # aria-haspopup="menu" trigger, its nearest media element. The
    # second piece is what disambiguates "two triggers on the page
    # which one is image-context vs video-context" without a separate
    # probe.
    try:
        visible_raw = await tab.evaluate(
            r"""
            (() => {
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const btns = Array.from(document.querySelectorAll('button'))
                    .filter(visible)
                    .filter(b => {
                        const aria = (b.getAttribute('aria-label') || '').trim();
                        const haspopup = (b.getAttribute('aria-haspopup') || '').trim();
                        return aria || haspopup;
                    });
                const items = btns
                    .map(b => ({
                        aria: b.getAttribute('aria-label') || '',
                        haspopup: b.getAttribute('aria-haspopup') || '',
                        text: (b.innerText || '').trim().slice(0, 40),
                    }))
                    .slice(0, 30);
                // For each haspopup="menu" trigger, find the nearest
                // visible large media element so the failure log says
                // which media each trigger pairs with.
                const triggers = btns.filter(
                    b => (b.getAttribute('aria-haspopup') || '') === 'menu'
                );
                const media = Array.from(document.querySelectorAll('img, video'))
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 200 && r.height > 200;
                    });
                const triggerMediaPairs = triggers.map(t => {
                    const tr = t.getBoundingClientRect();
                    const tx = tr.x + tr.width/2;
                    const ty = tr.y + tr.height/2;
                    let best = null;
                    let bestDist = Infinity;
                    for (const m of media) {
                        const r = m.getBoundingClientRect();
                        const dx = (r.x + r.width/2) - tx;
                        const dy = (r.y + r.height/2) - ty;
                        const d = Math.sqrt(dx*dx + dy*dy);
                        if (d < bestDist) {
                            bestDist = d;
                            best = m;
                        }
                    }
                    return {
                        trigger_aria: t.getAttribute('aria-label') || '',
                        trigger_pos: {x: Math.round(tx), y: Math.round(ty)},
                        nearest_media: best ? {
                            tag: best.tagName,
                            distance: Math.round(bestDist),
                        } : null,
                    };
                });
                return JSON.stringify({
                    buttons: items,
                    haspopup_menu_triggers: triggerMediaPairs,
                });
            })()
            """
        )
        diag = _json.loads(visible_raw) if isinstance(visible_raw, str) else None
    except Exception:
        diag = None
    raise GrokAPIError(
        "Could not open '...' post menu (no aria-haspopup='menu' button "
        f"located, or Radix trigger isn't firing). Tried aria-labels: "
        f"{sorted(_MENU_BUTTON_NAMES)} + spatial-proximity + top-level "
        f"fallback. Diagnostic dump: {diag!r}. "
        "haspopup_menu_triggers shows each candidate trigger paired "
        "with its nearest media element — useful when chain-root posts "
        "have separate image-context and video-context triggers and "
        "we're picking the wrong one."
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
        for el in result
        if el.get("role") == "menuitem"
    ]
