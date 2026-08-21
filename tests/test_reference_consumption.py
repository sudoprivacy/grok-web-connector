"""Unit tests for reference CONSUMPTION (ref:<id> in generation calls).

No creds/browser: mocks the UI helper and REST post-processing to exercise
routing + guardrails. The live end-to-end run is
tests/integration/test_workflows.py::test_reference_to_video.

Verified finding (see project_reference_consumption memory): Grok consumes
references ONLY in video generation (mediaGenInput.referenceToVideo). There
is no reference->image path, so create_image/edit_image reject 'ref:<id>'.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from grok_web import GrokClient
from grok_web.exceptions import GrokAPIError
from grok_web.models import ImageEditResult, ImageGenerationResult, VideoGenerationResult
from grok_web.prompt_parser import classify_image_source


def _client():
    return GrokClient.__new__(GrokClient)


def _run(coro):
    return asyncio.run(coro)


class TestClassify:
    def test_ref_scheme(self):
        assert classify_image_source("ref:60116c2c-fa70-435a-a01b-139dc4cf9364") == (
            "reference",
            "60116c2c-fa70-435a-a01b-139dc4cf9364",
        )

    def test_ref_distinct_from_file_and_post(self):
        assert classify_image_source("post:x") == ("post", "x")
        assert classify_image_source("file:x") == ("upload", "x")
        assert classify_image_source("./a.png") == ("file", "./a.png")


class TestCreateVideoRouting:
    def test_ref_routes_to_reference_helper(self):
        c = _client()
        sentinel = VideoGenerationResult(
            video_id="vid-1",
            source_post_id="vid-1",
            parent_post_id="vid-1",
            moderated=False,
            progress=100,
            mode="reference",
        )
        c._create_video_from_reference = AsyncMock(return_value=sentinel)
        # inert REST post-processing
        c._fetch_video_duration = AsyncMock(return_value=(None, None))
        c.get_post_details = AsyncMock(return_value=object())

        out = _run(
            c.create_video(
                {
                    "images": ["ref:abc-123"],
                    "prompt": "the hero waves",
                    "duration": "6s",
                    "resolution": "480p",
                }
            )
        )
        assert out is sentinel
        kwargs = c._create_video_from_reference.call_args.kwargs
        assert kwargs["reference_ids"] == ["abc-123"]
        assert kwargs["prompt"] == "the hero waves"
        assert kwargs["duration"] == 6  # "6s" -> 6
        assert kwargs["resolution"] == "480p"

    def test_multiple_refs_passed_through(self):
        c = _client()
        sentinel = VideoGenerationResult(
            video_id="vid-2",
            source_post_id="vid-2",
            parent_post_id="vid-2",
            moderated=True,
            progress=0,
            mode="reference",
        )
        c._create_video_from_reference = AsyncMock(return_value=sentinel)
        c._fetch_video_duration = AsyncMock(return_value=(None, None))
        c.get_post_details = AsyncMock(return_value=object())
        _run(c.create_video({"images": ["ref:a", "ref:b"], "prompt": "x"}))
        assert c._create_video_from_reference.call_args.kwargs["reference_ids"] == ["a", "b"]

    def test_mixing_ref_with_post_rejected(self):
        c = _client()
        with pytest.raises(ValueError, match="mix source types"):
            _run(c.create_video({"images": ["post:x", "ref:y"], "prompt": "p"}))


class TestReferenceHelperGuards:
    def test_missing_reference_id_raises(self):
        c = _client()
        c.list_references = AsyncMock(return_value=[{"reference_id": "other", "name": "N"}])
        with pytest.raises(GrokAPIError, match="reference id\\(s\\) not found"):
            _run(c._create_video_from_reference(reference_ids=["abc"], prompt="p"))


class TestImageEditReject:
    def test_create_image_rejects_ref(self):
        c = _client()
        with pytest.raises(GrokAPIError, match="video-only|only in video|create_video"):
            _run(c.create_image({"images": ["ref:abc"], "prompt": "a cat"}))

    def test_edit_image_rejects_ref(self):
        c = _client()
        with pytest.raises(GrokAPIError, match="video-only|only in video|create_video"):
            _run(c.edit_image({"images": ["post:src", "ref:abc"], "prompt": "make it red"}))

    def test_create_reference_rejects_ref_source(self):
        c = _client()
        with pytest.raises(
            GrokAPIError, match="ref:<id>.*not supported|not from another reference"
        ):
            _run(c.create_reference({"images": ["ref:abc"], "title": "T"}))


class _StopNavTab:
    """Tab stub whose evaluate() aborts create_image right after the
    in-Grok-delegation decision (the first thing create_image awaits on the
    tab is window.location.href), so we can assert delegation did/didn't fire
    without a real browser."""

    async def evaluate(self, *a, **k):
        raise RuntimeError("STOP-NAV")


class TestCreateImageInGrokImg2Img:
    """A single Grok-native ref ('post:<id>' / bare uuid) + prompt is served
    in-Grok via edit_image's imageToImage (no re-upload). Everything else
    (no prompt, multi-ref, local files) falls through to the upload path."""

    def _edit_result(self):
        return ImageEditResult(
            post_id="src",
            edit_prompt="p",
            images=[{"image_id": "out1", "image_url": "u", "moderated": False}],
            conversation_id="conv1",
        )

    def test_single_post_ref_delegates_in_grok(self):
        c = _client()
        c.edit_image = AsyncMock(return_value=self._edit_result())
        out = _run(c.create_image({"images": ["post:hero"], "prompt": "on a bench"}))
        c.edit_image.assert_awaited_once()
        sent = c.edit_image.call_args[0][0]
        assert sent["images"] == ["post:hero"]
        assert sent["prompt"] == "on a bench"
        assert isinstance(out, ImageGenerationResult)
        assert out.images[0]["image_id"] == "out1"
        assert out.conversation_id == "conv1"

    def test_bare_uuid_ref_delegates_in_grok(self):
        c = _client()
        c.edit_image = AsyncMock(return_value=self._edit_result())
        _run(c.create_image({"images": ["b2db5daf-7da7-4856-aac4-5c22b5c361c2"], "prompt": "x"}))
        assert c.edit_image.call_args[0][0]["images"] == [
            "post:b2db5daf-7da7-4856-aac4-5c22b5c361c2"
        ]

    def test_no_prompt_not_delegated(self):
        c = _client()
        c.edit_image = AsyncMock()
        c._ui_delay = 0
        c._persistence_hinted = True
        c._tab = _StopNavTab()
        with pytest.raises(RuntimeError, match="STOP-NAV"):
            _run(c.create_image({"images": ["post:hero"]}))
        c.edit_image.assert_not_awaited()

    def test_local_file_not_delegated(self):
        c = _client()
        c.edit_image = AsyncMock()
        c._ui_delay = 0
        c._persistence_hinted = True
        c._tab = _StopNavTab()
        with pytest.raises(RuntimeError, match="STOP-NAV"):
            _run(c.create_image({"images": ["./hero.png"], "prompt": "x"}))
        c.edit_image.assert_not_awaited()


class TestCreateImageMultiCompose:
    """Multiple all-Grok refs → in-Grok imageToImage COMPOSITION by id
    (_create_image_via_imagetoimage). Cold token surfaces a warming hint;
    mixed sets fall through; single-ref stays on the edit_image path."""

    def test_multi_grok_refs_compose_by_id(self):
        c = _client()
        c._create_image_via_imagetoimage = AsyncMock(
            return_value=[{"image_id": "c1", "image_url": "u", "moderated": False}]
        )
        c.edit_image = AsyncMock()
        out = _run(c.create_image({"images": ["post:a", "post:b"], "prompt": "two together"}))
        c._create_image_via_imagetoimage.assert_awaited_once()
        args = c._create_image_via_imagetoimage.call_args[0]
        assert args[0] == ["a", "b"]  # ordered ids passed as inputAssets
        assert args[1] == "two together"
        c.edit_image.assert_not_awaited()  # not the single-ref path
        assert isinstance(out, ImageGenerationResult)
        assert out.images[0]["image_id"] == "c1"

    def test_cold_token_raises_warming_hint(self):
        c = _client()

        class _ColdSnitch:
            _by_endpoint: dict = {}

            async def get(self, *a, **k):
                return None  # no signed conversations/new token cached

        c._statsig_snitch = _ColdSnitch()
        with pytest.raises(GrokAPIError, match="create_video|warm|warmed"):
            _run(c._create_image_via_imagetoimage(["a", "b"], "compose", 60))

    def test_multi_mixed_grok_and_local_not_composed(self):
        c = _client()
        c._create_image_via_imagetoimage = AsyncMock()
        c.edit_image = AsyncMock()
        c._ui_delay = 0
        c._persistence_hinted = True
        c._tab = _StopNavTab()
        with pytest.raises(RuntimeError, match="STOP-NAV"):
            _run(c.create_image({"images": ["post:a", "./b.png"], "prompt": "x"}))
        c._create_image_via_imagetoimage.assert_not_awaited()  # not all-Grok
        c.edit_image.assert_not_awaited()
