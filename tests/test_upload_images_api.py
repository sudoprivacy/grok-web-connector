"""Tests for upload_images() API surface and create_video() dispatch with file: refs."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grok_web.client import GrokClient
from grok_web.models import VideoGenerationResult


def _make_client_stub() -> GrokClient:
    """Build a GrokClient with initialization bypassed for dispatch tests."""
    c = GrokClient.__new__(GrokClient)
    c.cookies = MagicMock(x_userid="user-1")
    c._tab = MagicMock()
    c._ui_delay = 1.0
    c._statsig_snitch = MagicMock()
    return c


class TestVideoGenerationResultFields:
    """VideoGenerationResult must expose image_file_ids for retry flows."""

    def test_default_image_file_ids_empty(self):
        r = VideoGenerationResult(video_id="v1", parent_post_id="p1")
        assert r.image_file_ids == []

    def test_populated_image_file_ids_roundtrip(self):
        r = VideoGenerationResult(
            video_id="v1",
            parent_post_id="p1",
            image_file_ids=["f1", "f2", "f3"],
        )
        # Ensures field survives model_dump (used by pool.serialize)
        dumped = r.model_dump()
        assert dumped["image_file_ids"] == ["f1", "f2", "f3"]


class TestCreateVideoDispatch:
    """create_video must route file: refs to direct REST, post: refs to UI,
    raw paths to upload2vid, and reject mixes."""

    @pytest.mark.asyncio
    async def test_file_refs_go_to_direct_rest(self):
        c = _make_client_stub()
        stub = AsyncMock(
            return_value=VideoGenerationResult(video_id="v-direct", parent_post_id="pp")
        )
        with patch.object(GrokClient, "_create_video_from_file_ids", stub):
            # Patch helpers used for non-direct paths so unexpected dispatch fails loudly
            with patch.object(
                GrokClient,
                "_create_video_via_ui",
                AsyncMock(side_effect=AssertionError("should not hit UI path")),
            ):
                with patch.object(
                    GrokClient,
                    "_create_video_from_upload",
                    AsyncMock(side_effect=AssertionError("should not hit upload path")),
                ):
                    result = await c.create_video(
                        {
                            "images": ["file:aaa", "file:bbb"],
                            "prompt": "zoom @1 @2",
                            "resolution": "480p",
                            "duration": "6s",
                        }
                    )
        assert result.video_id == "v-direct"
        stub.assert_awaited_once()
        kwargs = stub.call_args.kwargs
        assert kwargs["file_ids"] == ["aaa", "bbb"]
        assert kwargs["prompt"] == "zoom @1 @2"

    @pytest.mark.asyncio
    async def test_post_refs_go_to_ui_path(self):
        c = _make_client_stub()
        stub = AsyncMock(return_value=VideoGenerationResult(video_id="v-ui", parent_post_id="pp"))
        with patch.object(GrokClient, "_create_video_via_ui", stub):
            with patch.object(
                GrokClient,
                "_create_video_from_file_ids",
                AsyncMock(side_effect=AssertionError("should not hit direct path")),
            ):
                await c.create_video(
                    {
                        "images": ["post:abc-123"],
                        "prompt": "go",
                    }
                )
        stub.assert_awaited_once()
        assert stub.call_args.kwargs["parent_post_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_mixed_types_raise(self):
        c = _make_client_stub()
        with pytest.raises(ValueError, match="Cannot mix"):
            await c.create_video(
                {
                    "images": ["file:xxx", "./local.jpg"],
                    "prompt": "p",
                }
            )

    @pytest.mark.asyncio
    async def test_raw_paths_go_to_upload_path(self):
        c = _make_client_stub()
        stub = AsyncMock(
            return_value=VideoGenerationResult(
                video_id="v-up", parent_post_id="pp", image_file_ids=["new-1"]
            )
        )
        with patch.object(GrokClient, "_create_video_from_upload", stub):
            r = await c.create_video({"images": ["./a.jpg"], "prompt": "x"})
        stub.assert_awaited_once()
        assert r.image_file_ids == ["new-1"]
