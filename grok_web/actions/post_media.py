"""Post media view actions — video/image view toggling.

Shared utilities for switching between video and image views on a Grok
Imagine post page (/imagine/post/{uuid}). Used by edit/extend flows that
need a specific viewport mode before opening the "..." menu (whose items
are context-sensitive: video items vs image items).

Why this is its own module: the chain-root edit_image case (image post
with video descendants) defaults to video viewport; the menu's items
follow viewport, not URL. So edit_image must switch to image mode before
opening the menu, and v0.19.7 made that explicit. v0.19.8 hardens the
switch itself — the toggle is a Radix Tabs button, click_by_ref doesn't
fire pointerdown, so the button "looked clicked" but never toggled.

Image-specific actions: see post_image.py
Video-specific actions: see post_video.py
"""

import asyncio
import json as _json
import logging

from ..exceptions import GrokAPIError

logger = logging.getLogger(__name__)


# Toggle button accessible names (zh + en).
_IMAGE_TOGGLE_NAMES = ("图片", "Image", "图像")
_VIDEO_TOGGLE_NAMES = ("视频", "Video")


async def _detect_rendered_mode(tab) -> str | None:
    """Detect viewport mode by looking at what's actually being rendered.

    More durable than toggle-state inspection — works even when the
    toggle UI hasn't hydrated yet, when aria-state lags behind, or when
    Grok renames buttons.

    Returns:
        ``"image"`` if the largest visible media element is an ``<img>``,
        ``"video"`` if it's a ``<video>``, or ``None`` if no visible
        media element was found (page may still be loading).
    """
    raw = await tab.evaluate(
        r"""
        (() => {
            // Largest visible <img> or <video> in the post detail area.
            // Filter out tiny thumbnails (sidebar strip) and elements
            // that are zero-size due to lazy-render.
            const candidates = Array.from(document.querySelectorAll('img, video'))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 200 && r.height > 200;
                });
            if (candidates.length === 0) return JSON.stringify({mode: null});
            candidates.sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (br.width * br.height) - (ar.width * ar.height);
            });
            const top = candidates[0];
            return JSON.stringify({
                mode: top.tagName === 'VIDEO' ? 'video' : 'image',
                tag: top.tagName,
                width: Math.round(top.getBoundingClientRect().width),
                height: Math.round(top.getBoundingClientRect().height),
            });
        })()
        """
    )
    try:
        parsed = _json.loads(raw) if isinstance(raw, str) else {}
    except (TypeError, ValueError):
        parsed = {}
    return parsed.get("mode")


async def get_media_view(tab) -> str | None:
    """Get the currently active media view ('video' or 'image').

    Detection priority:
      1. Largest visible media element (durable — what's actually shown).
      2. Toggle-button presence as a fallback signal.

    Returns:
        ``"video"``, ``"image"``, or ``None`` if neither signal yields
        a definitive answer (e.g., page still loading).
    """
    mode = await _detect_rendered_mode(tab)
    if mode in ("image", "video"):
        return mode

    # Fallback: enumerate toggle buttons. If neither toggle is present,
    # assume image-only post and return None (legacy semantics).
    from ai_dev_browser.core.snapshot import page_discover as page_find

    result = await page_find(tab, interactable_only=True)
    has_image_btn = False
    has_video_btn = False
    has_play_pause = False
    for el in result:
        name = el.get("name", "")
        if el.get("role") != "button":
            continue
        if name in _IMAGE_TOGGLE_NAMES:
            has_image_btn = True
        elif name in _VIDEO_TOGGLE_NAMES:
            has_video_btn = True
        elif name in ("播放", "暂停", "Play", "Pause"):
            has_play_pause = True

    if not has_image_btn and not has_video_btn:
        return None
    return "video" if has_play_pause else "image"


async def _click_toggle(tab, target: str, *, delay: float) -> str | None:
    """Click the image- or video-view toggle using JS pointer-dispatch.

    Grok's view toggle is a Radix Tabs button — like Radix DropdownMenu
    Trigger and MenuItem, it listens to ``pointerdown`` events, not
    plain ``click``. Neither ``click_by_ref`` (ax_tree) nor
    ``element.mouse_click()`` (CDP Input.dispatchMouseEvent without
    pointerType) fires a synthetic pointerdown that React sees, so
    those report success but Grok never toggles. Fix: dispatch the
    full pointer sequence (pointerdown / mousedown / pointerup /
    mouseup / click) via JS so React's Radix handlers register the
    interaction.

    Args:
        target: ``"image"`` or ``"video"``.
        delay: UI delay multiplier.

    Returns:
        The aria-label of the button we clicked (for logging), or None
        if no matching button was found on the page.
    """
    if target == "image":
        wanted = _IMAGE_TOGGLE_NAMES
    elif target == "video":
        wanted = _VIDEO_TOGGLE_NAMES
    else:
        raise ValueError(f"_click_toggle: target must be 'image' or 'video', got {target!r}")

    wanted_literal = _json.dumps(list(wanted))
    fired = await tab.evaluate(
        r"""
        (() => {
            const wanted = new Set(__WANTED__);
            // Visible buttons whose aria-label OR innerText matches.
            const all = Array.from(document.querySelectorAll('button')).filter(b => {
                const r = b.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const al = (b.getAttribute('aria-label') || '').trim();
                const tx = (b.innerText || '').trim();
                return wanted.has(al) || wanted.has(tx);
            });
            if (all.length === 0) return null;
            // Prefer the toggle that is NOT currently active (we want to
            // switch INTO target mode). Active toggles often have
            // data-state="active" or aria-selected="true".
            const inactive = all.find(b =>
                b.getAttribute('data-state') !== 'active' &&
                b.getAttribute('aria-selected') !== 'true'
            );
            const btn = inactive || all[0];
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
            return btn.getAttribute('aria-label') || (btn.innerText || '').trim() || '<unnamed>';
        })()
        """.replace("__WANTED__", wanted_literal)
    )
    if fired:
        await asyncio.sleep(0.6 * delay)
    return fired if isinstance(fired, str) else None


async def _diagnostic_dump(tab) -> dict:
    """Snapshot enough state for a switch-failure error message."""
    raw = await tab.evaluate(
        r"""
        (() => {
            const buttons = Array.from(document.querySelectorAll('button'))
                .filter(b => {
                    const r = b.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                })
                .filter(b => {
                    const al = (b.getAttribute('aria-label') || '').trim();
                    return al || b.getAttribute('aria-haspopup');
                })
                .map(b => ({
                    aria: b.getAttribute('aria-label') || '',
                    text: (b.innerText || '').trim().slice(0, 30),
                    state: b.getAttribute('data-state') || '',
                    selected: b.getAttribute('aria-selected') || '',
                }))
                .slice(0, 20);
            const media = Array.from(document.querySelectorAll('img, video'))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 100 && r.height > 100;
                })
                .map(el => {
                    const r = el.getBoundingClientRect();
                    return {tag: el.tagName, w: Math.round(r.width), h: Math.round(r.height)};
                })
                .sort((a, b) => (b.w * b.h) - (a.w * a.h))
                .slice(0, 5);
            return JSON.stringify({buttons, media});
        })()
        """
    )
    try:
        return _json.loads(raw) if isinstance(raw, str) else {}
    except (TypeError, ValueError):
        return {}


async def _switch_to(tab, target: str, *, delay: float) -> bool:
    """Generic switch-to-mode with verification + one retry.

    Drops the legacy "pre-check + skip if already there" fast path.
    For chain-root posts the pre-check is unreliable (page reports
    image-mode but viewport renders video), so always attempt the
    click and verify what's rendered after.

    Raises:
        GrokAPIError: If the post-click verification keeps failing —
            includes a diagnostic dump so the next failure is
            actionable without a separate probe script.
    """
    # If verification already passes, skip the click. This is cheap and
    # avoids unnecessary toggle clicks during repeat calls.
    rendered = await _detect_rendered_mode(tab)
    if rendered == target:
        return True

    for attempt in range(2):
        clicked_label = await _click_toggle(tab, target, delay=delay)
        if not clicked_label:
            # Toggle button absent. Could be:
            #   - Image-only post (no toggle) and we want image → success.
            #   - Image-only post and we want video → impossible.
            #   - Toggle UI hasn't hydrated yet → retry once.
            if attempt == 0:
                await asyncio.sleep(1.0 * delay)
                continue
            # No toggle on the second pass. Verify viewport.
            rendered = await _detect_rendered_mode(tab)
            if rendered == target:
                return True
            if rendered is None:
                # Page may not have loaded a media element at all — let
                # the caller decide; treat as success rather than raise.
                return True
            dump = await _diagnostic_dump(tab)
            raise GrokAPIError(
                f"switch_to_{target}_view: no toggle button found and "
                f"viewport is in {rendered!r} mode (wanted {target!r}). "
                f"Tried aria-labels: {list(_IMAGE_TOGGLE_NAMES if target == 'image' else _VIDEO_TOGGLE_NAMES)}. "
                f"Page state: {dump!r}"
            )

        # Click fired — wait for state propagation and verify.
        await asyncio.sleep(1.2 * delay)
        rendered = await _detect_rendered_mode(tab)
        if rendered == target:
            logger.debug(
                "[switch_to_%s_view] success on attempt %d (clicked %r, rendered %s)",
                target,
                attempt + 1,
                clicked_label,
                rendered,
            )
            return True
        logger.warning(
            "[switch_to_%s_view] attempt %d clicked %r but viewport "
            "still rendered as %r — retrying",
            target,
            attempt + 1,
            clicked_label,
            rendered,
        )

    # Both attempts failed. Dump state into the error so we can fix
    # without asking the reporter to run a separate probe.
    dump = await _diagnostic_dump(tab)
    raise GrokAPIError(
        f"switch_to_{target}_view: clicked toggle 2× but viewport "
        f"remained {rendered!r} mode. The toggle button is present but "
        f"not switching — Grok may have changed the Radix Tabs internals. "
        f"Page state: {dump!r}"
    )


async def switch_to_image_view(tab, *, delay: float = 1.0) -> bool:
    """Switch to image view; raises if it can't actually switch.

    Idempotent: returns immediately if the viewport already shows an
    image. Otherwise clicks the Radix Tabs toggle via JS pointer
    dispatch and verifies that the largest rendered media element
    became an ``<img>``. One retry on click-eaten-by-overlay.

    Raises:
        GrokAPIError: Toggle missing AND viewport is video-mode, or
            click fired twice and viewport never switched. Error
            includes a diagnostic dump (visible buttons + their state,
            largest media elements) so the next UI drift is actionable.
    """
    return await _switch_to(tab, "image", delay=delay)


async def switch_to_video_view(tab, *, delay: float = 1.0) -> bool:
    """Switch to video view; raises if it can't actually switch.

    Mirror of :func:`switch_to_image_view`. See its docstring for the
    failure semantics — same diagnostic mechanism.
    """
    return await _switch_to(tab, "video", delay=delay)
