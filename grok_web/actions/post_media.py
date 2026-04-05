"""Post media view actions — video/image view toggling.

Shared utilities for switching between video and image views
on a Grok Imagine post page (/imagine/post/{uuid}).

Image-specific actions: see post_image.py
Video-specific actions: see post_video.py (future)
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def get_media_view(tab) -> str | None:
    """Get the currently active media view ('video' or 'image').

    Returns:
        'video' if video view is active, 'image' if image view is active,
        None if neither toggle is found (e.g., image-only post with no videos).
    """
    from ai_dev_browser.core.snapshot import page_find

    # If play/pause button exists, video is showing.
    result = await page_find(tab, interactable_only=True)
    has_play_pause = False
    has_video_btn = False
    has_image_btn = False

    for el in result.get("elements", []):
        name = el.get("name", "")
        if el.get("role") == "button":
            if name in ("播放", "暂停", "Play", "Pause"):
                has_play_pause = True
            elif name in ("视频", "Video"):
                has_video_btn = True
            elif name in ("图片", "Image"):
                has_image_btn = True

    if not has_video_btn and not has_image_btn:
        return None  # No toggle — likely image-only post

    return "video" if has_play_pause else "image"


async def switch_to_image_view(tab, *, delay: float = 1.0) -> bool:
    """Switch to image view if not already showing images.

    Returns:
        True if switched (or already on image view)
    """
    from ai_dev_browser.core.ax import click_by_ref
    from ai_dev_browser.core.snapshot import page_find

    current = await get_media_view(tab)
    if current == "image" or current is None:
        return True  # Already on image view or no toggle

    result = await page_find(tab, text="图片", interactable_only=True)
    for el in result.get("elements", []):
        if el.get("role") == "button" and el.get("name") in ("图片", "Image"):
            await click_by_ref(tab, el["ref"])
            await asyncio.sleep(1.5 * delay)
            return True

    return False


async def switch_to_video_view(tab, *, delay: float = 1.0) -> bool:
    """Switch to video view if not already showing video.

    Returns:
        True if switched (or already on video view)
    """
    from ai_dev_browser.core.ax import click_by_ref
    from ai_dev_browser.core.snapshot import page_find

    current = await get_media_view(tab)
    if current == "video":
        return True  # Already on video view

    result = await page_find(tab, text="视频", interactable_only=True)
    for el in result.get("elements", []):
        if el.get("role") == "button" and el.get("name") in ("视频", "Video"):
            await click_by_ref(tab, el["ref"])
            await asyncio.sleep(1.5 * delay)
            return True

    return False
