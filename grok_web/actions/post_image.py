"""Post image actions — thumbnail selection on image view.

Operates on a Grok Imagine post page (/imagine/post/{uuid}) in image view.
Uses ai-dev-browser ax_tree for resilient element discovery.
"""

import asyncio
import logging

from ..exceptions import GrokAPIError

logger = logging.getLogger(__name__)


async def get_thumbnails(tab) -> list[dict]:
    """Get all image thumbnail buttons on the current post page.

    Returns:
        List of dicts sorted by index:
        [{"index": 1, "name": "Thumbnail 1", "ref": "35#6303"}, ...]
        Empty list if no thumbnails (single-image post).
    """
    from ai_dev_browser.core.snapshot import page_discover as page_find

    result = await page_find(tab, text="Thumbnail", interactable_only=True)
    thumbnails = []
    for el in result:
        if el.get("role") == "button" and el.get("name", "").startswith("Thumbnail"):
            name = el["name"]
            # Extract index from "Thumbnail 1", "Thumbnail 2", etc.
            try:
                index = int(name.split()[-1])
            except (ValueError, IndexError):
                index = len(thumbnails) + 1
            thumbnails.append({"index": index, "name": name, "ref": el["ref"]})

    thumbnails.sort(key=lambda t: t["index"])
    return thumbnails


async def select_thumbnail(tab, index: int, *, delay: float = 1.0) -> bool:
    """Select an image thumbnail by 1-based index.

    Args:
        tab: browser Tab instance
        index: 1-based thumbnail index (e.g., 1 for first image)
        delay: UI delay multiplier

    Returns:
        True if thumbnail was clicked

    Raises:
        GrokAPIError: If thumbnail not found
    """
    from ai_dev_browser.core.ax import click_by_ref
    from ai_dev_browser.core.snapshot import page_discover as page_find

    target_name = f"Thumbnail {index}"

    result = await page_find(tab, text=target_name, interactable_only=True)
    for el in result:
        if el.get("role") == "button" and el.get("name") == target_name:
            await click_by_ref(tab, el["ref"])
            await asyncio.sleep(1 * delay)
            return True

    raise GrokAPIError(f"Thumbnail {index} not found")
