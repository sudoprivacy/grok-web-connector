"""Lock in our reading of ai-dev-browser's page_discover return shape.

We've shipped this bug twice (v0.19.3→v0.19.4 a similar one for tab.evaluate;
v0.19.6→v0.19.7 for page_discover): assuming the return value is a dict
with an ``"elements"`` key when it's actually a ``list[dict]``.

This test mocks page_discover with the documented shape, then exercises
every helper that consumes it. If any helper regresses to ``.get("elements", [])``
the test crashes immediately rather than at the next live-Grok run.

This is NOT a Grok integration test — it's pure shape verification, fast.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

# Synthesized list-of-dicts the real page_discover returns. Includes the
# kinds of elements every helper looks for (Thumbnail buttons, image/video
# toggle buttons, play/pause for view detection, menuitems).
_FAKE_ELEMENTS = [
    {"role": "button", "name": "Thumbnail 1", "ref": "1#1001"},
    {"role": "button", "name": "Thumbnail 2", "ref": "1#1002"},
    {"role": "button", "name": "图片", "ref": "1#1003"},
    {"role": "button", "name": "视频", "ref": "1#1004"},
    {"role": "button", "name": "Pause", "ref": "1#1005"},
    {"role": "button", "name": "生成视频", "ref": "1#1006"},
    {"role": "menuitem", "name": "Custom", "ref": "1#1007"},
    {"role": "menuitem", "name": "扩展", "ref": "1#1008"},
]


class _FakeTab:
    """Stub tab — we never actually hit it from these helpers."""


@pytest.fixture
def fake_page_discover():
    """Patch every callsite's import of page_discover to return a list."""

    async def _fake(tab, text=None, interactable_only=True, **kwargs):
        # Mimic page_discover's text-filter behavior: substring match on name.
        if text:
            return [el for el in _FAKE_ELEMENTS if text in (el.get("name") or "")]
        return list(_FAKE_ELEMENTS)

    # ai-dev-browser is imported lazily inside each helper, so we patch the
    # source module — both `page_discover` and the `page_find` alias.
    with patch("ai_dev_browser.core.snapshot.page_discover", new=_fake):
        yield _fake


@pytest.fixture
def fake_click_by_ref():
    async def _fake(tab, ref):
        return {"clicked": True, "ref": ref}

    with patch("ai_dev_browser.core.ax.click_by_ref", new=_fake):
        yield _fake


def test_post_image_get_thumbnails(fake_page_discover):
    from grok_web.actions.post_image import get_thumbnails

    result = asyncio.run(get_thumbnails(_FakeTab()))
    assert result == [
        {"index": 1, "name": "Thumbnail 1", "ref": "1#1001"},
        {"index": 2, "name": "Thumbnail 2", "ref": "1#1002"},
    ]


def test_post_image_select_thumbnail(fake_page_discover, fake_click_by_ref):
    from grok_web.actions.post_image import select_thumbnail

    assert asyncio.run(select_thumbnail(_FakeTab(), 1, delay=0)) is True


def test_post_video_get_video_thumbnails(fake_page_discover):
    from grok_web.actions.post_video import get_video_thumbnails

    result = asyncio.run(get_video_thumbnails(_FakeTab()))
    assert {t["name"] for t in result} == {"Thumbnail 1", "Thumbnail 2"}


def test_post_video_select_video_thumbnail(fake_page_discover, fake_click_by_ref):
    from grok_web.actions.post_video import select_video_thumbnail

    assert asyncio.run(select_video_thumbnail(_FakeTab(), 2, delay=0)) is True


def test_post_media_get_media_view_video(fake_page_discover):
    # _FAKE_ELEMENTS has a "Pause" button → video view.
    from grok_web.actions.post_media import get_media_view

    assert asyncio.run(get_media_view(_FakeTab())) == "video"


def test_post_media_switch_to_image_view(fake_page_discover, fake_click_by_ref):
    from grok_web.actions.post_media import switch_to_image_view

    assert asyncio.run(switch_to_image_view(_FakeTab(), delay=0)) is True


def test_post_media_switch_to_video_view(fake_page_discover, fake_click_by_ref):
    # Already on video (Pause button present) → no-op success.
    from grok_web.actions.post_media import switch_to_video_view

    assert asyncio.run(switch_to_video_view(_FakeTab(), delay=0)) is True


def test_post_menu_get_menu_items(fake_page_discover):
    from grok_web.actions.post_menu import get_menu_items

    result = asyncio.run(get_menu_items(_FakeTab()))
    assert {it["name"] for it in result} == {"Custom", "扩展"}
