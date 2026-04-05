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

    Finds the menu button via ax_tree (role=button, name in known set)
    and clicks it. Assumes already navigated to a post page.

    Args:
        tab: browser Tab instance
        delay: UI delay multiplier

    Returns:
        True if menu was opened

    Raises:
        GrokAPIError: If menu button not found after retries
    """
    from ai_dev_browser.core.ax import click_by_ref
    from ai_dev_browser.core.snapshot import find

    for attempt in range(3):
        result = await find(tab, interactable_only=True)
        for el in result.get("elements", []):
            if el.get("role") == "button" and el.get("name") in _MENU_BUTTON_NAMES:
                await click_by_ref(tab, el["ref"])
                await asyncio.sleep(1 * delay)
                return True

        # Fallback: try CSS selectors (in case ax_tree doesn't expose the name)
        for selector in [
            'button[aria-label="更多选项"][aria-haspopup="menu"]',
            'button[aria-label="More options"][aria-haspopup="menu"]',
            'button[aria-label="Options"]',
        ]:
            try:
                btn = await tab.find(selector)
                if btn:
                    await btn.scroll_into_view()
                    await asyncio.sleep(0.5 * delay)
                    await btn.mouse_click()
                    await asyncio.sleep(1 * delay)
                    return True
            except Exception:
                pass

        if attempt < 2:
            await asyncio.sleep(2 * delay)

    raise GrokAPIError("Could not find '...' menu button (更多选项/Options)")


async def click_menu_item(tab, *text_options: str, delay: float = 1.0) -> bool:
    """Click a menu item by matching its accessible name.

    Uses ax_tree to find menuitems, with CSS fallback.

    Args:
        tab: browser Tab instance
        *text_options: Text strings to match (e.g., "延长视频", "Extend video")
        delay: UI delay multiplier

    Returns:
        True if item was clicked

    Raises:
        GrokAPIError: If no matching menu item found
    """
    from ai_dev_browser.core.ax import click_by_ref
    from ai_dev_browser.core.snapshot import find

    text_set = set(text_options)

    for attempt in range(3):
        # ax_tree approach: find all elements, filter menuitems by name
        result = await find(tab, interactable_only=True)
        for el in result.get("elements", []):
            if el.get("role") == "menuitem" and el.get("name") in text_set:
                await click_by_ref(tab, el["ref"])
                await asyncio.sleep(0.5 * delay)
                return True

        # CSS fallback: iterate [role="menuitem"] and match text
        try:
            items = await tab.find_all('[role="menuitem"]')
            for item in items:
                item_text = item.text.strip() if item.text else ""
                if item_text in text_set:
                    await item.scroll_into_view()
                    await asyncio.sleep(0.2 * delay)
                    await item.mouse_click()
                    await asyncio.sleep(0.5 * delay)
                    return True
        except Exception:
            pass

        if attempt < 2:
            await asyncio.sleep(1 * delay)

    raise GrokAPIError(f"Could not find menu item: {text_options}")


async def get_menu_items(tab) -> list[dict]:
    """Get all visible menu items via ax_tree.

    Useful for debugging — see what items are in the currently open menu.

    Returns:
        List of dicts with 'name', 'role', 'ref' for each menuitem
    """
    from ai_dev_browser.core.snapshot import find

    result = await find(tab, interactable_only=True)
    return [
        {"name": el.get("name", ""), "role": el.get("role"), "ref": el.get("ref")}
        for el in result.get("elements", [])
        if el.get("role") == "menuitem"
    ]
