"""Post video actions — video thumbnail selection on video view.

Operates on a Grok Imagine post page (/imagine/post/{uuid}) in video view.
Uses ai-dev-browser ax_tree for resilient element discovery.
"""

import asyncio
import logging

from ..exceptions import GrokAPIError

logger = logging.getLogger(__name__)


async def get_video_thumbnails(tab) -> list[dict]:
    """Get all video thumbnail buttons on the current post page (video view).

    Returns:
        List of dicts sorted by index:
        [{"index": 1, "name": "Thumbnail 1", "ref": "35#4221"}, ...]
        Empty list if only one video (no sidebar).
    """
    from ai_dev_browser.core.snapshot import page_discover as page_find

    result = await page_find(tab, text="Thumbnail", interactable_only=True)
    thumbnails = []
    for el in result.get("elements", []):
        if el.get("role") == "button" and el.get("name", "").startswith("Thumbnail"):
            name = el["name"]
            try:
                index = int(name.split()[-1])
            except (ValueError, IndexError):
                index = len(thumbnails) + 1
            thumbnails.append({"index": index, "name": name, "ref": el["ref"]})

    thumbnails.sort(key=lambda t: t["index"])
    return thumbnails


async def select_video_thumbnail(tab, index: int, *, delay: float = 1.0) -> bool:
    """Select a video thumbnail by 1-based index.

    Args:
        tab: browser Tab instance
        index: 1-based thumbnail index
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
    for el in result.get("elements", []):
        if el.get("role") == "button" and el.get("name") == target_name:
            await click_by_ref(tab, el["ref"])
            await asyncio.sleep(1 * delay)
            return True

    raise GrokAPIError(f"Video thumbnail {index} not found")
