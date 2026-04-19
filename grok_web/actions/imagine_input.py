"""Actions for the Grok Imagine homepage input bar (April 2026 new UI).

The new UI has a bottom input bar on grok.com/imagine with:
- Upload button + hidden file input (multiple images)
- Contenteditable text editor (tiptap/ProseMirror) with @ references
- Mode radios: 图片 / 视频
- Video options: 480p/720p, 6s/10s, aspect ratio dropdown
- Submit button (enabled after image upload)
"""

import asyncio
import logging
from pathlib import Path

from ai_dev_browser import cdp

from ..exceptions import GrokAPIError

logger = logging.getLogger(__name__)

BASE_URL = "https://grok.com"


async def navigate_to_imagine(tab, *, delay: float = 1.0) -> None:
    """Navigate to the Imagine homepage, forcing navigation unless already
    on the clean homepage.

    Skips navigation only if the URL is exactly /imagine (with optional
    trailing slash or query). Template pages (/imagine/templates/...),
    post pages (/imagine/post/...), and sub-routes all force a fresh
    navigation to avoid operating inside a modal or detail view where
    selectors match the wrong elements.

    Args:
        tab: browser Tab instance
        delay: UI delay multiplier
    """
    current_url = await tab.evaluate("window.location.href")
    # Normalize: strip query/hash, trailing slash
    path = current_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    on_clean_homepage = path.endswith("/imagine")
    if not on_clean_homepage:
        await tab.get(f"{BASE_URL}/imagine")
        await asyncio.sleep(2 * delay)


async def upload_image(
    tab,
    image_path: str | Path,
    *,
    timeout: int = 15,
    delay: float = 1.0,  # noqa: ARG001
) -> int:
    """Upload an image via the hidden file input on the Imagine homepage.

    The new UI (April 2026) no longer creates a post on upload. Instead,
    the image appears as a tag above the input bar.

    Args:
        tab: browser Tab instance
        image_path: Path to the local image file
        timeout: Max seconds to wait for upload confirmation
        delay: UI delay multiplier

    Returns:
        Number of images currently attached (e.g., 1 after first upload, 2 after second).

    Raises:
        FileNotFoundError: If the image file doesn't exist.
        GrokAPIError: If file input not found or upload times out.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    # Count existing images before upload
    before_count = await _count_uploaded_images(tab)

    # Find hidden file input and set file
    doc = await tab.send(cdp.dom.get_document(-1, True))
    node_id = await tab.send(
        cdp.dom.query_selector(doc.node_id, 'input[type="file"][name="files"]')
    )
    if not node_id:
        raise GrokAPIError("File input element not found on Imagine page")

    from ai_dev_browser.core._element import filter_recurse

    node = filter_recurse(doc, lambda n: n.node_id == node_id)
    if not node:
        raise GrokAPIError("Could not resolve file input node")

    await tab.send(
        cdp.dom.set_file_input_files(
            [str(image_path.absolute())], backend_node_id=node.backend_node_id
        )
    )

    # Wait for upload confirmation: new "Remove image" button appears
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        after_count = await _count_uploaded_images(tab)
        if after_count > before_count:
            logger.info(f"Image uploaded ({after_count} total): {image_path.name}")
            return after_count
        await asyncio.sleep(0.5)

    raise GrokAPIError(f"Upload timed out after {timeout}s — 'Remove image' button did not appear")


async def _count_uploaded_images(tab) -> int:
    """Count the number of uploaded images by counting 'Remove image' buttons."""
    result = await tab.evaluate(
        "document.querySelectorAll('button[aria-label=\"Remove image\"]').length",
        await_promise=False,
    )
    if not isinstance(result, int | float):
        logger.warning(f"_count_uploaded_images got {type(result).__name__}, returning 0")
        return 0
    return int(result)


async def check_moderated_images(tab) -> list[int]:
    """Check which uploaded images have been moderated by Grok.

    Moderated images show a warning triangle overlay (`.lucide-triangle-alert`)
    but remain in the UI with their 'Remove image' button.

    Finds image containers by walking up from each 'Remove image' button
    to avoid fragile CSS group selectors.

    Returns:
        List of 0-based indices of moderated images.
    """
    result = await tab.evaluate(
        """
        (function() {
            const buttons = document.querySelectorAll('button[aria-label="Remove image"]');
            const moderated = [];
            buttons.forEach(function(btn, i) {
                var container = btn.closest('.relative.shrink-0') || btn.parentElement;
                if (container && container.querySelector('.lucide-triangle-alert')) {
                    moderated.push(i);
                }
            });
            return moderated;
        })()
    """,
        await_promise=False,
    )
    # CDP may return ExceptionDetails instead of a list on JS errors
    if not isinstance(result, list):
        logger.warning(f"check_moderated_images got {type(result).__name__}, returning empty")
        return []
    # ai-dev-browser's deep serialization wraps list elements as
    # {"type": "number", "value": N}; unwrap to plain ints.
    indices = []
    for item in result:
        if isinstance(item, int):
            indices.append(item)
        elif isinstance(item, dict) and item.get("type") == "number":
            indices.append(int(item["value"]))
        else:
            logger.warning(f"check_moderated_images unexpected element: {item!r}")
    return indices


async def remove_moderated_images(tab, *, delay: float = 1.0) -> int:
    """Remove all moderated images from the input bar.

    Moderated images block submit. This removes them so remaining clean
    images can still be used.

    Returns:
        Number of moderated images removed.
    """
    max_iterations = 30
    removed = 0
    for _ in range(max_iterations):
        # Re-check each time since indices shift after removal
        moderated = await check_moderated_images(tab)
        if not moderated:
            return removed
        # Always remove the first moderated one (index shifts after each removal)
        idx = moderated[0]
        success = await tab.evaluate(
            f"""
            (function() {{
                var buttons = document.querySelectorAll('button[aria-label="Remove image"]');
                if ({idx} < buttons.length) {{
                    buttons[{idx}].click();
                    return true;
                }}
                return false;
            }})()
        """,
            await_promise=False,
        )
        if not success:
            return removed
        await asyncio.sleep(0.3 * delay)
        removed += 1
    logger.warning(
        "remove_moderated_images: %d click attempts did not clear moderated "
        "overlays; bailing out to avoid infinite loop.",
        max_iterations,
    )
    return removed


async def remove_all_images(tab, *, delay: float = 1.0) -> int:
    """Remove all uploaded images from the input bar.

    Returns:
        Number of images removed.

    Recovery: if clicks stop working (Grok disables the Remove buttons
    after anti-abuse triggers, say, or the tab is in a stale state
    after a raised exception in the previous call), the loop would
    spin forever on the same button. Bail out after ``max_iterations``
    clicks and force a full ``/imagine`` reload — that clears the
    upload tray deterministically.
    """
    max_iterations = 30
    removed = 0
    for _ in range(max_iterations):
        btn = await tab.query_selector('button[aria-label="Remove image"]')
        if not btn:
            return removed
        await btn.click()
        await asyncio.sleep(0.3 * delay)
        removed += 1

    # Fell through the cap — the click loop is not making progress.
    # Force a page reload to clear the tray rather than hang the caller.
    logger.warning(
        "remove_all_images: %d click attempts did not clear the upload tray "
        "(buttons still present after each click). Forcing /imagine reload "
        "to recover tab state.",
        max_iterations,
    )
    await tab.get(f"{BASE_URL}/imagine")
    await asyncio.sleep(2 * delay)
    return removed


async def get_current_mode(tab) -> str:
    """Get current generation mode ('图片' or '视频').

    Returns:
        '图片' or '视频', or '' if neither found.
    """
    result = await tab.evaluate(
        """
        (function() {
            const radios = document.querySelectorAll('[role="radio"]');
            for (const r of radios) {
                if (r.getAttribute('aria-checked') === 'true') {
                    const text = r.textContent.trim();
                    if (text === '图片' || text === '视频') return text;
                }
            }
            return '';
        })()
    """,
        await_promise=False,
    )
    return result or ""


async def set_mode(tab, mode: str, *, delay: float = 1.0) -> str:
    """Switch generation mode to '图片' or '视频'.

    Args:
        tab: browser Tab instance
        mode: '图片', '视频', 'image', or 'video'
        delay: UI delay multiplier

    Returns:
        The mode that is now active.
    """
    # Normalize
    mode_map = {"image": "图片", "video": "视频", "图片": "图片", "视频": "视频"}
    target = mode_map.get(mode.lower(), mode)

    current = await get_current_mode(tab)
    if current == target:
        return current

    # Find and click the target radio
    clicked = await tab.evaluate(
        f"""
        (function() {{
            const radios = document.querySelectorAll('[role="radio"]');
            for (const r of radios) {{
                if (r.textContent.trim() === '{target}') {{
                    r.click();
                    return true;
                }}
            }}
            return false;
        }})()
    """,
        await_promise=False,
    )

    if not clicked:
        raise GrokAPIError(f"Could not find mode radio for '{target}'")

    await asyncio.sleep(0.5 * delay)
    return await get_current_mode(tab)


async def set_video_options(
    tab,
    *,
    resolution: str | None = None,
    duration: int | None = None,
    aspect_ratio: str | None = None,
    delay: float = 1.0,
) -> None:
    """Set video generation options (resolution, duration, aspect ratio).

    Only sets options that are explicitly provided. Skips None values.

    Args:
        tab: browser Tab instance
        resolution: "480p" or "720p"
        duration: 6 or 10 (seconds)
        aspect_ratio: e.g., "2:3", "16:9"
        delay: UI delay multiplier
    """
    # Resolution radio
    if resolution:
        label = resolution if resolution.endswith("p") else f"{resolution}p"
        await _click_radio(tab, label)
        await asyncio.sleep(0.3 * delay)

    # Duration radio
    if duration:
        label = f"{duration}s"
        await _click_radio(tab, label)
        await asyncio.sleep(0.3 * delay)

    # Aspect ratio dropdown
    if aspect_ratio:
        ar_btn = await tab.query_selector('button[aria-label="宽高比"]')
        if not ar_btn:
            ar_btn = await tab.query_selector('button[aria-label="Aspect ratio"]')
        if ar_btn:
            await ar_btn.click()
            await asyncio.sleep(0.3 * delay)
            # Click the option in the dropdown
            option = await tab.evaluate(
                f"""
                (function() {{
                    const items = document.querySelectorAll('[role="option"], [role="menuitem"]');
                    for (const item of items) {{
                        if (item.textContent.trim().includes('{aspect_ratio}')) {{
                            item.click();
                            return true;
                        }}
                    }}
                    return false;
                }})()
            """,
                await_promise=False,
            )
            if not option:
                raise GrokAPIError(
                    f"Aspect ratio '{aspect_ratio}' not found in dropdown. "
                    "Available options may differ for multi-image uploads."
                )
            await asyncio.sleep(0.3 * delay)


async def _click_radio(tab, label: str) -> bool:
    """Click a radio button by its text label."""
    return await tab.evaluate(
        f"""
        (function() {{
            const radios = document.querySelectorAll('[role="radio"]');
            for (const r of radios) {{
                if (r.textContent.trim() === '{label}') {{
                    r.click();
                    return true;
                }}
            }}
            return false;
        }})()
    """,
        await_promise=False,
    )


async def set_prompt(tab, prompt: str, *, delay: float = 1.0) -> None:
    """Set the text prompt in the contenteditable editor.

    Args:
        tab: browser Tab instance
        prompt: Text to enter (replaces any existing text)
        delay: UI delay multiplier
    """
    escaped = prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    await tab.evaluate(
        f"""
        (function() {{
            const editor = document.querySelector('.tiptap.ProseMirror') ||
                           document.querySelector('[contenteditable="true"]');
            if (editor) {{
                editor.focus();
                editor.innerHTML = '<p>{escaped}</p>';
                editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                return true;
            }}
            return false;
        }})()
    """,
        await_promise=False,
    )
    await asyncio.sleep(0.3 * delay)


async def reference_image(tab, image_index: int, *, delay: float = 1.0) -> bool:
    """Type @ in the editor and select a specific image as reference.

    Args:
        tab: browser Tab instance
        image_index: 1-based image index (matches "Image 1", "Image 2", etc.)
        delay: UI delay multiplier

    Returns:
        True if the image was selected, False if not found.
    """
    # Focus editor and type @.
    # Use JS focus + insert_text instead of click + dispatch_key_event because:
    # - .click() on a ProseMirror DIV sometimes targets BODY
    # - dispatch_key_event("char") does not insert text into ProseMirror's internal
    #   model; insert_text sends an InputEvent which ProseMirror honors.
    editor_exists = await tab.evaluate(
        '!!(document.querySelector(".tiptap.ProseMirror") || '
        "document.querySelector('[contenteditable=\"true\"]'))",
        await_promise=False,
    )
    if not editor_exists:
        raise GrokAPIError("Contenteditable editor not found")

    await tab.evaluate(
        '(document.querySelector(".tiptap.ProseMirror") || '
        "document.querySelector('[contenteditable=\"true\"]')).focus()",
        await_promise=False,
    )
    await asyncio.sleep(0.2 * delay)
    await tab.send(cdp.input_.insert_text(text="@"))
    await asyncio.sleep(0.5 * delay)

    # Wait for and click "Image N" button
    target_name = f"Image {image_index}"
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < 3:
        btn = await tab.evaluate(
            f"""
            (function() {{
                const buttons = document.querySelectorAll('button');
                for (const b of buttons) {{
                    if (b.textContent.trim() === '{target_name}') {{
                        b.click();
                        return true;
                    }}
                }}
                return false;
            }})()
        """,
            await_promise=False,
        )
        if btn:
            await asyncio.sleep(0.3 * delay)
            return True
        await asyncio.sleep(0.3)

    logger.warning(f"Image reference '{target_name}' not found after @")
    return False


async def set_prompt_with_refs(
    tab,
    segments: list[dict],
    *,
    delay: float = 1.0,
) -> None:
    """Set prompt with interleaved text and @N image references.

    Clears the editor, then for each segment:
    - text → insertText via execCommand
    - ref  → type '@', wait for selector popup, click 'Image N'

    Args:
        tab: browser Tab instance
        segments: Parsed segments from prompt_parser.parse_prompt().
            Each is {"type": "text", "value": "..."} or {"type": "ref", "index": N}.
        delay: UI delay multiplier
    """
    # Focus and clear editor
    editor = await tab.query_selector(".tiptap.ProseMirror")
    if not editor:
        editor = await tab.query_selector('[contenteditable="true"]')
    if not editor:
        raise GrokAPIError("Contenteditable editor not found")

    await editor.click()
    await asyncio.sleep(0.2 * delay)

    # Clear existing content
    await tab.evaluate(
        """
        (function() {
            const editor = document.querySelector('.tiptap.ProseMirror') ||
                           document.querySelector('[contenteditable="true"]');
            if (editor) {
                editor.focus();
                editor.innerHTML = '';
                editor.dispatchEvent(new Event('input', { bubbles: true }));
            }
        })()
    """,
        await_promise=False,
    )
    await asyncio.sleep(0.3 * delay)

    for segment in segments:
        if segment["type"] == "text":
            # Insert text via execCommand (respects ProseMirror state)
            text = segment["value"]
            escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
            await tab.evaluate(
                f"document.execCommand('insertText', false, '{escaped}')",
                await_promise=False,
            )
            await asyncio.sleep(0.1 * delay)

        elif segment["type"] == "ref":
            # Use existing reference_image logic
            index = segment["index"]
            success = await reference_image(tab, index, delay=delay)
            if not success:
                raise GrokAPIError(
                    f"Image reference @{index} not found in @ menu. "
                    "The image may have been moderated — check with check_moderated_images()."
                )

    await asyncio.sleep(0.3 * delay)


async def is_submit_enabled(tab) -> bool:
    """Check if the submit button is enabled."""
    return await tab.evaluate(
        """
        (function() {
            const btn = document.querySelector('button[aria-label="提交"]');
            if (!btn) {
                // Try English
                const btn2 = document.querySelector('button[aria-label="Submit"]');
                return btn2 ? !btn2.disabled : false;
            }
            return !btn.disabled;
        })()
    """,
        await_promise=False,
    )


async def click_submit(tab, *, delay: float = 1.0) -> None:
    """Click the submit button to trigger generation.

    Raises:
        GrokAPIError: If submit button not found or disabled.
    """
    btn = await tab.query_selector('button[aria-label="提交"]')
    if not btn:
        btn = await tab.query_selector('button[aria-label="Submit"]')
    if not btn:
        raise GrokAPIError("Submit button not found")

    # Check if enabled
    disabled = await tab.evaluate(
        """
        (function() {
            const btn = document.querySelector('button[aria-label="提交"]') ||
                        document.querySelector('button[aria-label="Submit"]');
            return btn ? btn.disabled : true;
        })()
    """,
        await_promise=False,
    )
    if disabled:
        raise GrokAPIError("Submit button is disabled (no image uploaded?)")

    await btn.click()
    await asyncio.sleep(0.5 * delay)
