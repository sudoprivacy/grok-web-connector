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
from grok_web.models import VideoGenerationResult
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
