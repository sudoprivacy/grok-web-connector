"""Regression: edit_image must fail loud on chain-root, not silently
fall back to video generation.

Two prior bugs combined to make chain-root edit_image silently mutate
accounts:

  1. The 图片-mode-switch step in ``edit_current`` blind-fired the
     click and didn't verify the panel actually flipped from
     video-context to image-context. On chain-root posts (image posts
     with video descendants) the 图片 tab can be missing entirely.

  2. The submit step had a silent fallback: if the 编辑 button was
     missing, it clicked 生成视频 instead. Combined with (1), this
     meant calling ``edit_image`` on a chain-root would silently
     generate a VIDEO using the edit prompt — wrong API semantics,
     account mutation, AND the caller waits ``timeout`` (default
     300s) for an image NDJSON response that never arrives because
     the actual response was a video stream.

The fix is three defensive layers, asserted here as code-level
contracts (no browser needed):

  - Layer 1: ``_edit_image_via_ui`` raises if select_post couldn't
    keep us on the requested post.
  - Layer 2: ``edit_current`` raises immediately after the 图片-mode
    switch if the panel still lacks a 编辑/Edit button.
  - Layer 3: ``edit_current``'s submit step has NO 生成视频 fallback.
"""

from __future__ import annotations

import inspect

import pytest

from grok_web.client import GrokClient


def _edit_current_source() -> str:
    return inspect.getsource(GrokClient.edit_current)


def _edit_image_via_ui_source() -> str:
    return inspect.getsource(GrokClient._edit_image_via_ui)


def test_via_ui_asserts_select_post_landing():
    """Layer 1: post-select_post URL must contain post_id, else raise."""
    src = _edit_image_via_ui_source()
    assert "location.href" in src, (
        "_edit_image_via_ui must check location.href after select_post "
        "to catch the chain-root re-redirect case"
    )
    assert "post_id not in str(landed_url)" in src or (
        "if post_id not in" in src and "landed" in src
    ), (
        "_edit_image_via_ui must raise GrokAPIError if select_post couldn't "
        "stay on post_id (chain-root re-redirect defense)"
    )


def test_edit_current_verifies_image_mode_switch():
    """Layer 2: After the 图片-tab click, mode must be verified."""
    src = _edit_current_source()
    # Must read panel state after the mode switch
    assert "has_edit" in src and "mode_state" in src, (
        "edit_current must evaluate panel state (has_edit / mode_state) "
        "after the 图片-tab click, not blind-fire the switch"
    )
    # Must raise loudly on missing 编辑 button
    assert "video-generation mode" in src or "panel locks to video" in src, (
        "edit_current must raise an explicit chain-root error when the "
        "panel didn't flip to image-edit mode"
    )


def test_edit_current_submit_has_no_video_fallback():
    """Layer 3: submit step must NOT fall back to 生成视频."""
    src = _edit_current_source()
    # '生成视频' may legitimately appear in the mode_state check or
    # in the error message; what we forbid is the legacy submit-step
    # fallback, which had a distinct shape: a second .find() against
    # the 生成视频 label used as a click target.
    legacy_fallback_marker = ".find(x => (x.getAttribute('aria-label')||'')==='生成视频')"
    assert legacy_fallback_marker not in src, (
        "edit_current's submit step still falls back to 生成视频 — that "
        "fallback silently generates a video using the edit prompt on "
        "chain-root posts. Remove it; rely on the mode-state check above."
    )


def test_edit_current_submit_uses_multilocale_edit_label():
    """The submit click must accept both 编辑 and Edit."""
    src = _edit_current_source()
    assert "['编辑', 'Edit']" in src or "'Edit'" in src, (
        "edit_current's submit click should accept both '编辑' and 'Edit' "
        "so English-locale sessions work too"
    )


@pytest.mark.asyncio
async def test_via_ui_raises_when_select_post_returns_wrong_url(monkeypatch):
    """Behavioral lock for layer 1: even if select_post returns without
    raising, _edit_image_via_ui must fail loud if location.href doesn't
    contain post_id. This is the user's reported scenario — select_post's
    'landed-via-large-img' check matches a sidebar preview thumbnail of
    the chain-root image (which renders on every descendant page) and
    returns 'success' without actually updating the route.
    """
    from grok_web import exceptions

    POST_ID = "11111111-1111-1111-1111-111111111111"
    DESCENDANT_URL = "https://grok.com/imagine/post/22222222-2222-2222-2222-222222222222"

    class FakeTab:
        async def evaluate(self, js, **kwargs):
            return DESCENDANT_URL

    client = GrokClient.__new__(GrokClient)
    client._tab = FakeTab()

    async def fake_select_post(self, params, **kwargs):
        return  # returns success without updating URL

    async def fake_edit_current(self, *args, **kwargs):
        raise AssertionError(
            "edit_current must not be called when select_post left us on the wrong URL"
        )

    monkeypatch.setattr(GrokClient, "select_post", fake_select_post)
    monkeypatch.setattr(GrokClient, "edit_current", fake_edit_current)

    with pytest.raises(exceptions.GrokAPIError) as exc_info:
        await client._edit_image_via_ui(POST_ID, "any prompt", timeout=10)

    msg = str(exc_info.value)
    assert POST_ID in msg, "error must name the post id"
    assert "chain-root" in msg, "error must explain the chain-root cause"
    assert "reference image" in msg, "error must mention the workaround"
