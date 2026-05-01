"""Lock in our reading of ai-dev-browser's return shapes.

Two recurring bug classes that this test file guards against:

1. ``page_discover`` returns ``list[dict]``, not ``{"elements": [...]}``.
   We shipped 10 broken callsites of ``result.get("elements", [])``
   before v0.19.7 caught it. Re-introducing that pattern → instant fail.

2. ``tab.evaluate`` returns whatever the JS expression yields. Our
   helpers wrap returns in ``JSON.stringify`` and parse back; if a
   helper regresses to expecting an auto-deserialized dict, the
   FakeTab here surfaces it.

Pure shape verification — no live Grok needed, runs in <1s.
"""

from __future__ import annotations

import asyncio
import json as _json
from unittest.mock import patch

import pytest

from grok_web.exceptions import GrokAPIError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
_FAKE_PAGE_DISCOVER_ELEMENTS = [
    {"role": "button", "name": "Thumbnail 1", "ref": "1#1001"},
    {"role": "button", "name": "Thumbnail 2", "ref": "1#1002"},
    {"role": "button", "name": "图片", "ref": "1#1003"},
    {"role": "button", "name": "视频", "ref": "1#1004"},
    {"role": "button", "name": "Pause", "ref": "1#1005"},
    {"role": "button", "name": "生成视频", "ref": "1#1006"},
    {"role": "menuitem", "name": "Custom", "ref": "1#1007"},
    {"role": "menuitem", "name": "扩展", "ref": "1#1008"},
]


class _StubTab:
    """Tab stub for helpers that don't call evaluate() (page_discover only)."""


class _ScriptedTab:
    """Tab stub for helpers that drive Radix via tab.evaluate.

    Configure with a js_responder callable: receives the JS source and
    returns whatever the helper expects (typically a JSON string).
    """

    def __init__(self, js_responder):
        self.js_responder = js_responder
        self.evaluate_calls: list[str] = []

    async def evaluate(self, js, **_kwargs):
        self.evaluate_calls.append(js)
        return self.js_responder(js)


def _classify_js(js: str) -> str:
    """Classify which helper sent this JS so the responder can answer
    appropriately. The helpers in post_media.py are the main consumer."""
    if "buttons" in js and "media" in js and "JSON.stringify" in js:
        return "diagnostic_dump"
    if "'img, video'" in js:
        return "detect_rendered_mode"
    if "PointerEvent" in js and "pointerdown" in js:
        return "click_toggle"
    return "unknown"


def _make_responder(*, modes, click_aria="Image", dump=None):
    """Build a JS responder for post_media tests.

    Args:
        modes: Iterable of values to return from detect_rendered_mode
            (one per call). Use ``"image"``, ``"video"``, or ``None``.
        click_aria: Value returned by _click_toggle (the aria-label
            string of the button we "clicked"). Use ``None`` to simulate
            "no toggle button found".
        dump: Optional dict returned by _diagnostic_dump.
    """
    mode_iter = iter(modes)
    dump = dump or {"buttons": [], "media": []}

    def responder(js: str):
        kind = _classify_js(js)
        if kind == "diagnostic_dump":
            return _json.dumps(dump)
        if kind == "detect_rendered_mode":
            try:
                m = next(mode_iter)
            except StopIteration:
                m = None
            return _json.dumps({"mode": m})
        if kind == "click_toggle":
            return click_aria
        return None

    return responder


@pytest.fixture
def fake_page_discover():
    """Patch page_discover at the source so every callsite sees the list shape."""

    async def _fake(_tab, text=None, interactable_only=True, **_kw):
        if text:
            return [el for el in _FAKE_PAGE_DISCOVER_ELEMENTS if text in (el.get("name") or "")]
        return list(_FAKE_PAGE_DISCOVER_ELEMENTS)

    with patch("ai_dev_browser.core.snapshot.page_discover", new=_fake):
        yield _fake


@pytest.fixture
def fake_click_by_ref():
    async def _fake(_tab, ref):
        return {"clicked": True, "ref": ref}

    with patch("ai_dev_browser.core.ax.click_by_ref", new=_fake):
        yield _fake


# ---------------------------------------------------------------------------
# page_discover shape — 6 callsites still use it as fallback / primary
# ---------------------------------------------------------------------------
def test_post_image_get_thumbnails(fake_page_discover):
    from grok_web.actions.post_image import get_thumbnails

    result = asyncio.run(get_thumbnails(_StubTab()))
    assert result == [
        {"index": 1, "name": "Thumbnail 1", "ref": "1#1001"},
        {"index": 2, "name": "Thumbnail 2", "ref": "1#1002"},
    ]


def test_post_image_select_thumbnail(fake_page_discover, fake_click_by_ref):
    from grok_web.actions.post_image import select_thumbnail

    assert asyncio.run(select_thumbnail(_StubTab(), 1, delay=0)) is True


def test_post_video_get_video_thumbnails(fake_page_discover):
    from grok_web.actions.post_video import get_video_thumbnails

    result = asyncio.run(get_video_thumbnails(_StubTab()))
    assert {t["name"] for t in result} == {"Thumbnail 1", "Thumbnail 2"}


def test_post_video_select_video_thumbnail(fake_page_discover, fake_click_by_ref):
    from grok_web.actions.post_video import select_video_thumbnail

    assert asyncio.run(select_video_thumbnail(_StubTab(), 2, delay=0)) is True


def test_post_menu_get_menu_items(fake_page_discover):
    from grok_web.actions.post_menu import get_menu_items

    result = asyncio.run(get_menu_items(_StubTab()))
    assert {it["name"] for it in result} == {"Custom", "扩展"}


# ---------------------------------------------------------------------------
# post_media.py — JS-dispatching helpers (Radix Tabs require pointerdown)
# ---------------------------------------------------------------------------
def test_switch_to_image_view_skips_click_when_already_image():
    """Fast path: viewport already in image mode, no click needed."""
    from grok_web.actions.post_media import switch_to_image_view

    tab = _ScriptedTab(_make_responder(modes=["image"]))
    assert asyncio.run(switch_to_image_view(tab, delay=0)) is True
    # No click should have been dispatched.
    click_calls = [c for c in tab.evaluate_calls if _classify_js(c) == "click_toggle"]
    assert click_calls == []


def test_switch_to_image_view_clicks_then_verifies():
    """Initial mode=video → click → verify→image → success."""
    from grok_web.actions.post_media import switch_to_image_view

    tab = _ScriptedTab(_make_responder(modes=["video", "image"]))
    assert asyncio.run(switch_to_image_view(tab, delay=0)) is True
    # Exactly one click expected.
    click_calls = [c for c in tab.evaluate_calls if _classify_js(c) == "click_toggle"]
    assert len(click_calls) == 1


def test_switch_to_image_view_retries_then_raises_with_diagnostic():
    """Click 2× but viewport stays video → raise GrokAPIError with dump."""
    from grok_web.actions.post_media import switch_to_image_view

    dump = {
        "buttons": [{"aria": "图片", "text": "", "state": "", "selected": ""}],
        "media": [{"tag": "VIDEO", "w": 800, "h": 600}],
    }
    # mode sequence: initial=video, post-click1=video, post-click2=video
    tab = _ScriptedTab(_make_responder(modes=["video", "video", "video"], dump=dump))
    with pytest.raises(GrokAPIError) as excinfo:
        asyncio.run(switch_to_image_view(tab, delay=0))
    msg = str(excinfo.value)
    assert "remained" in msg.lower()
    # Diagnostic dump must be in the error so we can debug without a probe script.
    assert "VIDEO" in msg or "图片" in msg
    # 2 click attempts expected.
    click_calls = [c for c in tab.evaluate_calls if _classify_js(c) == "click_toggle"]
    assert len(click_calls) == 2


def test_switch_to_image_view_no_toggle_but_already_image():
    """Image-only post (no toggle) and viewport renders image → success."""
    from grok_web.actions.post_media import switch_to_image_view

    # First detect: video (need to click). Click finds no button (None).
    # Retry: click still no button. Then verify renders image → success.
    tab = _ScriptedTab(_make_responder(modes=["video", "image"], click_aria=None))
    assert asyncio.run(switch_to_image_view(tab, delay=0)) is True


def test_switch_to_video_view_symmetric():
    """Switch to video mirrors switch_to_image — same machinery."""
    from grok_web.actions.post_media import switch_to_video_view

    tab = _ScriptedTab(_make_responder(modes=["image", "video"], click_aria="视频"))
    assert asyncio.run(switch_to_video_view(tab, delay=0)) is True


def test_get_media_view_uses_rendered_mode_first():
    """Primary signal is largest rendered media, not toggle button presence."""
    from grok_web.actions.post_media import get_media_view

    tab = _ScriptedTab(_make_responder(modes=["video"]))
    assert asyncio.run(get_media_view(tab)) == "video"
    # Should NOT have fallen through to page_discover (we didn't even patch it).


def test_get_media_view_falls_back_to_page_discover_when_no_rendered_media(fake_page_discover):
    """If no <img>/<video> > 200px exists yet, fall back to toggle inspection."""
    from grok_web.actions.post_media import get_media_view

    # Detect returns mode=null → fallback path runs page_discover.
    tab = _ScriptedTab(_make_responder(modes=[None]))
    # _FAKE_PAGE_DISCOVER_ELEMENTS includes Pause + 视频/图片 toggles
    # → Pause present → "video".
    assert asyncio.run(get_media_view(tab)) == "video"
