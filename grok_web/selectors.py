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
from collections.abc import Awaitable, Callable

# Track which signal files have been cleaned up this process
_signal_cleaned_paths: set[str] = set()


def select_all() -> Callable[[int, Callable], Awaitable[list[int]]]:
    """Select all generated images.

    Returns a selector that selects all items without user interaction.
    Useful for automated workflows where you want all non-moderated images.

    Returns:
        Selector function that returns all indices.

    Example:
        result = await client.create_image("a cat", thumbnail_selector=select_all())
    """

    async def selector(item_count: int, scan_favorites) -> list[int]:  # noqa: ARG001
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


def auto_favorite_first_n(
    n: int = 1,
) -> Callable[[int, Callable], Awaitable[list[int]]]:
    """Auto-click the "Create Video" button on the first N gallery items.

    On Grok's 2026-04 Imagine UI, clicking "生成视频" on a gallery image
    does two things at once: it fires POST /rest/media/post/like (which
    **persists the temporary gallery image as a real post** under the
    user's account) and begins the img2vid generation. ``create_image``
    already listens for those like requests and captures the post_ids
    — this selector just drives the UI click.

    The "保存" button on the same card does NOT persist the image on
    its own (despite the aria-label); it's a soft-favorite-in-gallery
    that doesn't mint a post. Only 生成视频 persists.

    Use this when you want a persistent image post to feed into
    :meth:`edit_image` or
    ``create_video({"images": ["post:<id>"], ...})``.

    Args:
        n: How many gallery items to persist (default 1).

    Example::

        from grok_web import auto_favorite_first_n
        res = await client.create_image({
            "prompt": "a cat",
            "thumbnail_selector": auto_favorite_first_n(1),
        })
        persistent_post_id = res.selected_post_ids[0]

    Note: this will also start video generation for each persisted
    image (Grok's UI couples the two). If you only want the image
    post and not the video, you can ignore the generation result —
    the post_id is captured as soon as the like request fires, which
    happens before the video render kicks off.
    """

    async def selector(item_count: int, scan_favorites) -> list[int]:  # noqa: ARG001
        """Click 生成视频 on the first N gallery items, with retry.

        The click flow is inherently race-y — Grok's gallery transitions
        (fade-in, thumbnail loading, lazy layout) make the first click
        occasionally no-op. We retry up to 3 times per item, scrolling
        the specific button into view each attempt and re-snapshotting
        the ax tree to get a fresh ref.
        """
        import logging

        from ai_dev_browser.core import click_by_ref, page_discover

        _log = logging.getLogger(__name__)
        tab = scan_favorites.__self__._tab  # type: ignore[attr-defined]

        # Scroll gallery to top so the first items are in view.
        await tab.evaluate(
            r"""
            (() => {
                document.querySelectorAll('*').forEach(el => {
                    if (el.scrollTop > 0) el.scrollTop = 0;
                });
                window.scrollTo(0, 0);
            })()
            """
        )
        await asyncio.sleep(0.8)

        async def _click_gallery_item(index: int) -> bool:
            """Try to click the index-th 生成视频 button. Retries up to 3
            times, watching the handler-tracked persist count as the
            ground-truth signal for success.
            """
            before_persist = scan_favorites.__self__._captured_persist_count()  # type: ignore[attr-defined]
            for attempt in range(3):
                # Scroll + re-snapshot each attempt.
                await tab.evaluate(
                    f"""
                    (() => {{
                        const btns = Array.from(document.querySelectorAll('button'))
                            .filter(b => (b.getAttribute('aria-label')||'') === '生成视频');
                        if (btns.length > {index}) {{
                            btns[{index}].scrollIntoView({{block: 'center', behavior: 'instant'}});
                        }}
                    }})()
                    """
                )
                await asyncio.sleep(0.6)
                r2 = await page_discover(tab, text="生成视频", interactable_only=True)
                fresh = [
                    el
                    for el in r2.get("elements", [])
                    if el.get("role") == "button" and el.get("name") == "生成视频"
                ]
                if index >= len(fresh):
                    _log.warning(f"auto_favorite_first_n: item {index} not in snapshot")
                    return False
                ref = fresh[index]["ref"]
                r = await click_by_ref(tab, ref)
                _log.info(
                    f"auto_favorite_first_n: item {index} attempt {attempt} "
                    f"clicked={r.get('clicked')} ref={ref}"
                )
                # Wait for the /create + /like round-trip. Grok typically
                # responds within 1-2 s.
                await asyncio.sleep(2.5)
                after = scan_favorites.__self__._captured_persist_count()  # type: ignore[attr-defined]
                if after > before_persist:
                    return True
                _log.warning(
                    f"auto_favorite_first_n: item {index} attempt {attempt} "
                    "fired no persist requests — retrying"
                )
            return False

        # Grok's gallery occasionally serves items whose 生成视频 button
        # silently no-ops — typically those that are still finishing
        # their own render, even though the WebSocket reports
        # progress=100. Walk through items until we've persisted `n`
        # successfully, or exhaust the gallery.
        persisted = 0
        seen = 0
        max_scan = 12
        while persisted < n and seen < max_scan:
            ok = await _click_gallery_item(seen)
            seen += 1
            if ok:
                persisted += 1
                _log.info(f"auto_favorite_first_n: persisted {persisted}/{n}")
            else:
                _log.warning(
                    f"auto_favorite_first_n: item {seen - 1} unresponsive; " f"trying next"
                )

        return await scan_favorites()

    return selector


def signal_file_selector(
    signal_path: str = "C:/tmp/done",
) -> Callable[[int, Callable], Awaitable[list[int]]]:
    """Wait for a signal file to be created, then collect favorited items.

    Waits indefinitely for the user to create a signal file, allowing
    unlimited time for selection. Safe for multi-worker scenarios - the
    signal file is only cleaned at the start of a new process run.

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
        print("Click hearts on images you want to select.")
        print(f"When done, run:  echo . > {signal_path}")
        print("=" * 50 + "\n")

        # Remove old signal file only once per path per process (first worker to enter)
        if signal_path not in _signal_cleaned_paths:
            _signal_cleaned_paths.add(signal_path)
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

        # Note: Don't delete signal file here - other workers may still need it
        # The file will be deleted at the start of the next run

        return favorited

    return selector
