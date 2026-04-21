"""Filmstrip seed selector for video-extend.

When a user clicks ``扩展`` in the post "..." menu, Grok renders an extend
panel with a prompt editor at the bottom plus a 32px-tall filmstrip
overlay on the video showing the full (concatenated) chain timeline.
The filmstrip contains a 3px-bordered selection window anchored at
``left: 0`` and a right-edge handle (``div.cursor-ew-resize``). Dragging
the handle updates an inline ``width: N.NNN%`` style on the window;
width% × chain-duration = ``seed_start`` in seconds. A ``+6s`` / ``+10s``
toggle next to ``退出扩展模式`` picks the window length.

Two non-obvious requirements make this work:

1. **Focus emulation.** Chrome drops ``Input.dispatchMouseEvent``
   mousePressed / mouseReleased on background windows — only
   mouseMoved survives. Call :func:`enable_focus_emulation` before any
   drag so ``document.hasFocus()`` is true and CDP press/release emit
   their DOM events normally.
2. **Buttons held across drag.** React's drag handlers stop honouring
   mousemoves when the event's ``buttons`` bitmask is 0. The default
   ``Tab.mouse_drag`` helper omits ``buttons`` on intermediate moves,
   which makes the handle only follow the FIRST step and then stop. Use
   :func:`drag_seed_handle` — it passes ``buttons=1`` on every step.
"""

from __future__ import annotations

import asyncio
import json
import logging

from ai_dev_browser import cdp

from ..exceptions import GrokAPIError


async def _eval_json(tab, js: str):
    """tab.evaluate returns CDP-flavored objects for complex results — wrap
    the page expression in JSON.stringify and parse the string here so we
    always get plain Python dict/list/None.
    """
    raw = await tab.evaluate(f"JSON.stringify({js})")
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw == "null":
            return None
        return json.loads(raw)
    # Already a primitive or pre-parsed
    return raw


logger = logging.getLogger(__name__)

# Defaults tuned for 1920x1385 viewport observed in our test harness.
# Caller can pass real dimensions after the extend panel is open.
SEED_DRIFT_TOLERANCE = 1.0  # seconds; UI displays integer seconds, so >1s = bug
_HANDLE_POLL_INTERVAL = 0.05
_DRAG_STEPS = 12  # intermediate mouseMoved events to look like a human drag


async def _cdp_mouse(
    tab,
    type_: str,
    x: float,
    y: float,
    *,
    button: str = "none",
    click_count: int = 0,
    buttons: int = 0,
) -> None:
    """Send a single Input.dispatchMouseEvent over CDP with pointerType=mouse.

    ``buttons`` is the bitmask of currently-pressed buttons (1 = left);
    set it during drag-move events so React/Radix see pointer capture.
    """
    btn_map = {
        "none": cdp.input_.MouseButton.NONE,
        "left": cdp.input_.MouseButton.LEFT,
    }
    await tab.send(
        cdp.input_.dispatch_mouse_event(
            type_=type_,
            x=float(x),
            y=float(y),
            button=btn_map[button],
            click_count=click_count,
            buttons=buttons,
            pointer_type="mouse",
        )
    )


async def enable_focus_emulation(tab) -> None:
    """Make the tab behave as focused regardless of OS window focus.

    Chrome drops ``mousePressed`` / ``mouseReleased`` CDP input events
    when the target window lacks OS focus (``document.hasFocus() ==
    False``). ``mouseMoved`` still works, which is why UIs driven only by
    hover look fine while click / drag silently fail. Enabling
    ``Emulation.setFocusEmulationEnabled`` forces ``document.hasFocus()``
    to return true for the page and makes CDP input dispatch complete
    button-press and button-release DOM events even in background windows.
    """
    await tab.send(cdp.emulation.set_focus_emulation_enabled(enabled=True))


async def select_duration(tab, duration: str) -> bool:
    """Click the ``+6s`` / ``+10s`` toggle in the extend panel.

    Grok's current extend UI exposes window length as two small buttons
    next to the "退出扩展模式" control. Clicking them adjusts the
    selection window on the filmstrip accordingly. No-op if ``duration``
    is None. Warns (and returns False) on unknown values.
    """
    if not duration:
        return False
    label = f"+{duration.rstrip('s')}s"  # normalize '6s' → '+6s'
    fired = await tab.evaluate(
        r"""
        (() => {
            const want = "__LABEL__";
            const b = Array.from(document.querySelectorAll('button'))
                .find(x => (x.innerText||'').trim() === want);
            if (!b) return null;
            const r = b.getBoundingClientRect();
            const x = r.x + r.width/2, y = r.y + r.height/2;
            const o = {bubbles:true, cancelable:true, clientX:x, clientY:y,
                       pointerType:'mouse', button:0, pointerId:1, isPrimary:true};
            b.dispatchEvent(new PointerEvent('pointerdown', o));
            b.dispatchEvent(new MouseEvent('mousedown', o));
            b.dispatchEvent(new PointerEvent('pointerup', o));
            b.dispatchEvent(new MouseEvent('mouseup', o));
            b.dispatchEvent(new MouseEvent('click', o));
            return want;
        })()
        """.replace("__LABEL__", label)
    )
    if not fired:
        logger.warning(
            f"select_duration: button '{label}' not found — "
            "Grok may have renamed it or dropped this duration."
        )
        return False
    await asyncio.sleep(0.2)
    return True


async def wait_for_filmstrip(
    tab,
    *,
    timeout: float = 5.0,
    video_duration_hint: float | None = None,
) -> dict:
    """Wait until the filmstrip DOM is rendered and return its geometry.

    Returns a dict with keys::

        filmstrip_rect: [x, y, w, h]       — the 58-canvas strip container
        handle_rect:    [x, y, w, h]       — the ew-resize drag handle
        video_duration: float              — seconds from <video>.duration

    ``video_duration_hint`` — Grok's ``<video>`` uses HLS and may stay at
    ``readyState: 0`` (duration: NaN) indefinitely on a post page. Pass a
    known-good duration from the post's REST metadata to avoid waiting
    for the element to hydrate.
    """
    # Grok's <video> uses HLS and stays at readyState 0 on a post page, so
    # its .duration is NaN. If the caller didn't pass a hint, bootstrap
    # the duration by clicking +6s (which moves the handle to a known
    # position = total - 6s) and reading the resulting time display.
    deadline = asyncio.get_event_loop().time() + timeout
    last_data = None
    bootstrap_tried = False
    while asyncio.get_event_loop().time() < deadline:
        data = await _eval_json(
            tab,
            r"""
            (() => {
                // filmstrip container — h-[32px] div that holds the
                // ew-resize handle. Don't require <canvas> children;
                // Grok renders thumbnails lazily and the container
                // exists before video metadata loads.
                // Grok renders multiple ew-resize cursors — the actual
                // drag handle is the <div>, not its child SVG/path.
                // Prefer the RIGHTMOST div (bigger x), which corresponds
                // to the "end of selection" edge.
                const ewRes = Array.from(document.querySelectorAll('div'))
                    .filter(el => window.getComputedStyle(el).cursor === 'ew-resize');
                if (ewRes.length === 0) return null;
                const handle = ewRes.sort((a, b) =>
                    b.getBoundingClientRect().x - a.getBoundingClientRect().x
                )[0];
                // Walk up to find the h-[32px] container
                let container = handle;
                while (container && container !== document.body) {
                    const cls = (container.className || '').toString();
                    if (cls.includes('h-[32px]') && cls.includes('cursor-pointer')) break;
                    container = container.parentElement;
                }
                if (!container || container === document.body) {
                    // fallback: use the handle's parent's parent
                    container = handle.parentElement ? handle.parentElement.parentElement : null;
                    if (!container) return null;
                }
                const v = document.querySelector('video');
                const cr = container.getBoundingClientRect();
                const hr = handle ? handle.getBoundingClientRect() : null;
                // Derive duration from <video>.duration when available,
                // else from the M:SS → M:SS spans (end time) combined
                // with the selection window's width%. This is the only
                // reliable fallback before Grok's video element loads.
                let dur = (v && isFinite(v.duration)) ? v.duration : null;
                if (dur === null) {
                    // Fallback: derive from seed-start time + window width%.
                    // The selection window runs from 0 to seed_start, so
                    // width_pct / 100 == seed_start / total_duration.
                    const spans = Array.from(document.querySelectorAll('span'))
                        .map(s => (s.innerText||'').trim())
                        .filter(t => /^\d{1,2}:\d{2}$/.test(t));
                    const win = Array.from(document.querySelectorAll('div'))
                        .find(d => {
                            const cls = (d.className||'').toString();
                            const style = d.getAttribute('style') || '';
                            return /border-\[3px\]/.test(cls)
                                && /width:\s*[\d.]+%/.test(style);
                        });
                    if (spans.length >= 1 && win) {
                        const [m, s] = spans[0].split(':').map(Number);
                        const seedSec = m*60 + s;
                        const pctMatch = win.getAttribute('style').match(/width:\s*([\d.]+)%/);
                        if (pctMatch && seedSec > 0) {
                            const pct = parseFloat(pctMatch[1]);
                            if (pct > 0) dur = seedSec / (pct / 100);
                        }
                    }
                }
                return {
                    filmstrip_rect: [cr.x|0, cr.y|0, cr.width|0, cr.height|0],
                    handle_rect:    hr ? [hr.x|0, hr.y|0, hr.width|0, hr.height|0] : null,
                    video_duration: dur,
                };
            })()
            """,
        )
        if data:
            last_data = data
            if data.get("handle_rect"):
                if not data.get("video_duration") and video_duration_hint:
                    data["video_duration"] = video_duration_hint
                if data.get("video_duration"):
                    return data
                # Bootstrap: nudge the handle to a known position via +6s
                # so we can read back a seed time, then derive duration.
                if not bootstrap_tried:
                    bootstrap_tried = True
                    await select_duration(tab, "6s")
                    await asyncio.sleep(0.3)
                    continue
        await asyncio.sleep(_HANDLE_POLL_INTERVAL)
    raise GrokAPIError(
        "wait_for_filmstrip: filmstrip DOM not ready within timeout. "
        f"Last snapshot: {last_data!r}. (Symptom: extend panel opened but "
        "filmstrip container / handle / video.duration never became ready. "
        "Pass video_duration_hint from post metadata to bypass the "
        "<video> readiness wait.)"
    )


async def drag_seed_handle(
    tab,
    *,
    filmstrip_rect: list[int],
    handle_rect: list[int],
    video_duration: float,
    seed_start: float,
) -> None:
    """Drag the filmstrip's right handle so the selection window ends at
    ``seed_start`` seconds.

    The selection window is anchored at ``left: 0``; its inline ``width``
    (in %) equals ``seed_start / video_duration`` after the drag settles.
    We interpolate mouse movement over several steps so Grok's drag
    handler sees intermediate ``mousemove`` events (it batches the final
    position in ``onMouseUp`` — jumping directly sometimes loses the
    update).

    No-op if ``seed_start`` is None or the handle is already within one
    pixel of the target.
    """
    if seed_start is None:
        return
    if not (0 <= seed_start <= video_duration):
        raise GrokAPIError(
            f"seed_start {seed_start:.2f}s is out of range (source video is {video_duration:.2f}s)"
        )
    fx, _fy, fw, _fh = filmstrip_rect
    target_x = int(fx + (seed_start / video_duration) * fw)
    src_x = handle_rect[0] + handle_rect[2] // 2

    if abs(target_x - src_x) < 1:
        return

    fx = filmstrip_rect[0]
    fw = filmstrip_rect[2]
    strip_y = filmstrip_rect[1] + filmstrip_rect[3] // 2

    logger.debug(
        f"drag_seed_handle: seed_start={seed_start}s dur={video_duration}s "
        f"filmstrip={filmstrip_rect} handle={handle_rect} target_x={target_x}"
    )
    # When the selection window is at width: 0% (initial fresh-mount
    # state), the handle sits at filmstrip-left with its ``-right-[3px]``
    # positioning putting most of its 20px width OUTSIDE the container
    # — clipped by ``overflow: hidden``. Press ON THE HANDLE at its
    # visible overlap with the filmstrip (a few pixels at fx..fx+3).
    safe_src_x = max(src_x, fx + 3)
    safe_src_y = strip_y

    # ai-dev-browser's Tab.mouse_drag omits ``buttons`` on the stepped
    # mouseMoved events, which leaves the synthetic DOM events with
    # ``buttons: 0``. React / Radix drag handlers stop honouring
    # mousemoves when ``buttons`` drops to 0 (they interpret it as an
    # unexpected release), so the handle only follows the FIRST step
    # and then ignores the rest. Drive the drag ourselves with
    # ``buttons=1`` held down across every intermediate move.
    await _cdp_mouse(
        tab,
        "mousePressed",
        safe_src_x,
        safe_src_y,
        button="left",
        click_count=1,
        buttons=1,
    )
    await asyncio.sleep(0.08)
    for i in range(1, _DRAG_STEPS + 1):
        xi = safe_src_x + (target_x - safe_src_x) * i / _DRAG_STEPS
        await _cdp_mouse(
            tab,
            "mouseMoved",
            xi,
            safe_src_y,
            button="left",
            buttons=1,
        )
        await asyncio.sleep(0.03)
    await _cdp_mouse(
        tab,
        "mouseReleased",
        target_x,
        safe_src_y,
        button="left",
        click_count=1,
        buttons=0,
    )
    await asyncio.sleep(0.4)
    probe = await read_actual_seed_start(tab, video_duration=video_duration)
    logger.debug(
        f"drag ({safe_src_x},{safe_src_y}) → ({target_x},{safe_src_y}): "
        f"actual={probe['actual']}s width_pct={probe['width_pct']} "
        f"displayed={probe['displayed']}"
    )
    _ = fw  # available for future scroll-aware clamping


async def read_actual_seed_start(tab, *, video_duration: float) -> dict:
    """Read back the current seed position after a drag.

    Returns::

        {"actual": float | None,      # seconds, from inline width%
         "displayed": int | None,     # seconds, from M:SS span text
         "width_pct": float | None}
    """
    data = (
        await _eval_json(
            tab,
            r"""
        (() => {
            const win = Array.from(document.querySelectorAll('div'))
                .find(d => {
                    const cls = (d.className||'').toString();
                    const style = d.getAttribute('style') || '';
                    return /border-\[3px\]/.test(cls) && /width:\s*[\d.]+%/.test(style);
                });
            let pct = null;
            if (win) {
                const m = (win.getAttribute('style')||'').match(/width:\s*([\d.]+)%/);
                if (m) pct = parseFloat(m[1]);
            }
            // M:SS spans
            const spans = Array.from(document.querySelectorAll('span'))
                .map(s => (s.innerText||'').trim())
                .filter(t => /^\d{1,2}:\d{2}$/.test(t));
            let displayed = null;
            if (spans.length > 0) {
                const [m1, s1] = spans[0].split(':').map(Number);
                displayed = m1 * 60 + s1;
            }
            return {width_pct: pct, displayed: displayed};
        })()
        """,
        )
        or {}
    )
    pct = data.get("width_pct")
    actual = None
    if pct is not None and video_duration:
        actual = round(pct / 100.0 * video_duration, 3)
    return {
        "actual": actual,
        "displayed": data.get("displayed"),
        "width_pct": pct,
    }


async def fill_prompt(tab, prompt: str) -> None:
    """Type ``prompt`` into the extend panel's tiptap ProseMirror editor.

    Grok pre-fills the editor with the prior chain's prompt; if the caller
    passes a value here, we clear first. Uses ``execCommand('insertText')``
    because tiptap rejects direct ``.value`` / ``textContent`` writes.
    """
    if prompt is None:
        return  # keep UI's pre-filled text
    js_prompt = prompt.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
    await tab.evaluate(
        f"""
        (() => {{
            const ed = document.querySelector('div.tiptap.ProseMirror');
            if (!ed) return 'no-editor';
            ed.focus();
            // select all then delete
            document.execCommand('selectAll');
            document.execCommand('delete');
            document.execCommand('insertText', false, `{js_prompt}`);
            return 'ok';
        }})()
        """
    )
    await asyncio.sleep(0.1)


async def click_generate(tab) -> None:
    """Click the 生成视频 button to submit the extend request."""
    rect = await _eval_json(
        tab,
        r"""
        (() => {
            const b = Array.from(document.querySelectorAll('button'))
                .find(x => (x.getAttribute('aria-label')||'') === '生成视频');
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return {x: r.x|0, y: r.y|0, w: r.width|0, h: r.height|0};
        })()
        """,
    )
    if not rect:
        raise GrokAPIError("click_generate: 生成视频 button not found")
    cx = rect["x"] + rect["w"] // 2
    cy = rect["y"] + rect["h"] // 2
    await _cdp_mouse(tab, "mouseMoved", cx, cy)
    await asyncio.sleep(0.05)
    await _cdp_mouse(tab, "mousePressed", cx, cy, button="left", click_count=1)
    await asyncio.sleep(0.04)
    await _cdp_mouse(tab, "mouseReleased", cx, cy, button="left", click_count=1)
