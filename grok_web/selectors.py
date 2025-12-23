"""Thumbnail selector utilities for create_image().

These are pre-built selector callbacks for the `thumbnail_selector` parameter
in `create_image()`. They provide different ways to select which generated
images to collect post_ids for.

Usage:
    from grok_web import get_client, signal_file_selector

    async with get_client() as client:
        result = await client.create_image(
            "a cat",
            thumbnail_selector=signal_file_selector("C:/tmp/done")
        )
        print(result.selected_post_ids)

Note: Post IDs are captured automatically via network interception when
the user clicks the heart/favorite button in the browser. The selector
just determines when to stop waiting for user input.
"""

import asyncio
import os
from typing import Callable, Awaitable


def select_all() -> Callable[[int, Callable], Awaitable[list[int]]]:
    """Select all generated images.

    Returns a selector that selects all items without user interaction.
    Useful for automated workflows where you want all non-moderated images.

    Returns:
        Selector function that returns all indices.

    Example:
        result = await client.create_image("a cat", thumbnail_selector=select_all())
    """

    async def selector(item_count: int, scan_favorites) -> list[int]:
        return list(range(item_count))

    return selector


def timeout_selector(
    seconds: int = 30, message: str | None = None
) -> Callable[[int, Callable], Awaitable[list[int]]]:
    """Wait for a fixed timeout, then collect favorited items.

    Gives the user a fixed amount of time to click hearts on images
    they want to select. After the timeout, scans for favorited items.

    Args:
        seconds: How long to wait (default 30 seconds)
        message: Optional custom message to display

    Returns:
        Selector function.

    Example:
        result = await client.create_image(
            "a cat",
            thumbnail_selector=timeout_selector(60)  # 60 second timeout
        )
    """

    async def selector(item_count: int, scan_favorites) -> list[int]:
        msg = message or f"Click hearts on images you want. Auto-continuing in {seconds}s..."
        print(f"\n[Selection] {item_count} images available. {msg}")

        await asyncio.sleep(seconds)

        favorited = await scan_favorites()
        if favorited:
            print(f"[Selection] Found {len(favorited)} favorited items")
        else:
            print("[Selection] No favorites detected (post_ids captured via network)")

        return favorited

    return selector


def signal_file_selector(
    signal_path: str = "C:/tmp/done",
) -> Callable[[int, Callable], Awaitable[list[int]]]:
    """Wait for a signal file to be created, then collect favorited items.

    Waits indefinitely for the user to create a signal file, allowing
    unlimited time for selection. The signal file is deleted after reading.

    Args:
        signal_path: Path to the signal file (default: C:/tmp/done)

    Returns:
        Selector function.

    Example:
        result = await client.create_image(
            "a cat",
            thumbnail_selector=signal_file_selector()  # Uses default path
        )
        # User clicks hearts, then runs: echo . > C:/tmp/done
    """

    async def selector(item_count: int, scan_favorites) -> list[int]:
        print(f"\n{'=' * 50}")
        print(f"[Selection] {item_count} images available")
        print(f"Click hearts on images you want to select.")
        print(f"When done, run:  echo . > {signal_path}")
        print("=" * 50 + "\n")

        # Remove old signal file
        if os.path.exists(signal_path):
            os.remove(signal_path)

        # Wait for signal file
        while not os.path.exists(signal_path):
            await asyncio.sleep(2)

        print("[Selection] Signal received, collecting selections...")

        # Scan favorites
        favorited = await scan_favorites()
        if favorited:
            print(f"[Selection] Found {len(favorited)} favorited items: {favorited}")
        else:
            print("[Selection] No favorites detected (post_ids captured via network)")

        # Clean up signal file
        if os.path.exists(signal_path):
            os.remove(signal_path)

        return favorited

    return selector