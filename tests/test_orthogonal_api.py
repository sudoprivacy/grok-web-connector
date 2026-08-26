"""Unit tests for the v0.20 orthogonal generation API.

No creds/browser: mocks the verified internals to exercise the routing and
guardrails of the new role-explicit methods (img2img / compose / animate /
reference_video) and the deprecation warnings on the old overloaded params.

Each new method is a thin router onto an already-live-verified internal path
(see tests/integration/test_workflows.py for the e2e runs); these tests pin
the translation (role param -> images= form + _internal suppression) and the
Failure->hint guards. See docs/API_v0.20_redesign.md.
"""

from __future__ import annotations

import asyncio
import warnings
from unittest.mock import AsyncMock

import pytest

from grok_web import GrokClient
from grok_web.exceptions import GrokConfigError
from grok_web.models import ImageEditResult, ImageGenerationResult, VideoGenerationResult


def _client():
    return GrokClient.__new__(GrokClient)


def _run(coro):
    return asyncio.run(coro)


def _img_result():
    return ImageGenerationResult(
        prompt="p",
        images=[{"image_id": "out", "image_url": "u", "moderated": False}],
        conversation_id="conv",
    )


def _vid_result():
    return VideoGenerationResult(
        video_id="vid",
        source_post_id="vid",
        parent_post_id="vid",
        moderated=False,
        progress=100,
        mode="reference",
    )


class TestImg2ImgRouting:
    def test_source_routes_to_create_image_internal(self):
        c = _client()
        cap = {}

        async def fake(params, *, progress_callback=None, _internal=False):
            cap["params"] = params
            cap["_internal"] = _internal
            return _img_result()

        c.create_image = fake
        out = _run(c.img2img({"source": "post:hero", "prompt": "on a bench", "quality": "v2"}))
        assert cap["params"]["images"] == ["post:hero"]
        assert cap["params"]["prompt"] == "on a bench"
        assert cap["params"]["quality"] == "v2"
        assert cap["_internal"] is True
        assert isinstance(out, ImageGenerationResult)

    def test_missing_source_raises_config(self):
        c = _client()
        with pytest.raises(GrokConfigError, match="source.*required|compose"):
            _run(c.img2img({"prompt": "x"}))


class TestComposeRouting:
    def test_sources_route_to_create_image_internal(self):
        c = _client()
        cap = {}

        async def fake(params, *, progress_callback=None, _internal=False):
            cap["params"] = params
            cap["_internal"] = _internal
            return _img_result()

        c.create_image = fake
        _run(c.compose({"sources": ["post:a", "post:b"], "prompt": "two together"}))
        assert cap["params"]["images"] == ["post:a", "post:b"]
        assert cap["params"]["prompt"] == "two together"
        assert cap["_internal"] is True

    def test_fewer_than_two_raises_config(self):
        c = _client()
        with pytest.raises(GrokConfigError, match="2\\+|img2img"):
            _run(c.compose({"sources": ["a"], "prompt": "x"}))

    def test_local_source_rejected(self):
        c = _client()
        with pytest.raises(GrokConfigError, match="in-Grok|not a Grok-native"):
            _run(c.compose({"sources": ["post:a", "./b.png"], "prompt": "x"}))


class TestAnimateRouting:
    def _patch_video_postproc(self, c):
        c._fetch_video_duration = AsyncMock(return_value=(None, None))
        c.get_post_details = AsyncMock(return_value=object())

    def test_frame_routes_to_create_video_internal(self):
        c = _client()
        cap = {}

        async def fake(params, *, _internal=False):
            cap["params"] = params
            cap["_internal"] = _internal
            return _vid_result()

        c.create_video = fake
        _run(c.animate({"frame": "post:hero", "prompt": "slow zoom", "duration": "6s"}))
        assert cap["params"]["images"] == ["post:hero"]
        assert cap["params"]["prompt"] == "slow zoom"
        assert cap["params"]["duration"] == "6s"
        assert cap["_internal"] is True

    def test_missing_frame_raises_config(self):
        c = _client()
        with pytest.raises(GrokConfigError, match="frame.*required|reference_video"):
            _run(c.animate({"prompt": "x"}))


class TestReferenceVideoRouting:
    def test_references_route_to_create_video_internal(self):
        c = _client()
        cap = {}

        async def fake(params, *, _internal=False):
            cap["params"] = params
            cap["_internal"] = _internal
            return _vid_result()

        c.create_video = fake
        _run(c.reference_video({"references": ["r1", "r2"], "prompt": "waves", "duration": "6s"}))
        assert cap["params"]["images"] == ["ref:r1", "ref:r2"]
        assert cap["params"]["prompt"] == "waves"
        assert cap["_internal"] is True

    def test_empty_references_raises_config(self):
        c = _client()
        with pytest.raises(GrokConfigError, match="references.*required|create_reference"):
            _run(c.reference_video({"prompt": "x"}))


class TestDeprecations:
    """Overloaded params still work but warn; role methods do NOT warn."""

    def _edit_result(self):
        return ImageEditResult(
            post_id="src",
            edit_prompt="p",
            images=[{"image_id": "o", "image_url": "u", "moderated": False}],
            conversation_id="c",
        )

    def test_create_image_images_warns(self):
        c = _client()
        c.edit_image = AsyncMock(return_value=self._edit_result())
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _run(c.create_image({"images": ["post:hero"], "prompt": "x"}))
        assert any(
            issubclass(x.category, DeprecationWarning) and "img2img" in str(x.message) for x in w
        )

    def test_img2img_does_not_warn(self):
        c = _client()
        # Real create_image runs (with _internal=True); patch the downstream
        # so no browser is needed. img2img must suppress the deprecation.
        c.edit_image = AsyncMock(return_value=self._edit_result())
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _run(c.img2img({"source": "post:hero", "prompt": "x"}))
        assert not any(issubclass(x.category, DeprecationWarning) for x in w)

    def test_create_video_images_warns(self):
        c = _client()
        c._create_video_from_reference = AsyncMock(return_value=_vid_result())
        c._fetch_video_duration = AsyncMock(return_value=(None, None))
        c.get_post_details = AsyncMock(return_value=object())
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _run(c.create_video({"images": ["ref:r1"], "prompt": "x"}))
        assert any(
            issubclass(x.category, DeprecationWarning) and "animate" in str(x.message) for x in w
        )

    def test_reference_video_does_not_warn(self):
        c = _client()
        c._create_video_from_reference = AsyncMock(return_value=_vid_result())
        c._fetch_video_duration = AsyncMock(return_value=(None, None))
        c.get_post_details = AsyncMock(return_value=object())
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _run(c.reference_video({"references": ["r1"], "prompt": "x"}))
        assert not any(issubclass(x.category, DeprecationWarning) for x in w)

    def test_animate_post_warns(self):
        c = _client()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # warns first, then GrokConfigError (images required) — both expected
            with pytest.raises(GrokConfigError):
                _run(c.animate_post({}))
        assert any(
            issubclass(x.category, DeprecationWarning) and "animate" in str(x.message) for x in w
        )
