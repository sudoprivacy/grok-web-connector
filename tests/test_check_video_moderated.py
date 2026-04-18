"""Tests for check_video_moderated and create_video(verify_final=True)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grok_web.client import MODERATED_THUMBNAIL_UUID, GrokClient
from grok_web.models import VideoGenerationResult


def _stub_client() -> GrokClient:
    c = GrokClient.__new__(GrokClient)
    c.cookies = MagicMock(x_userid="u1")
    c._tab = MagicMock()
    c._ui_delay = 1.0
    c._statsig_snitch = MagicMock()
    return c


class TestCheckVideoModerated:
    @pytest.mark.asyncio
    async def test_clean_video_returns_false(self):
        c = _stub_client()
        c._api_request = AsyncMock(
            return_value={
                "post": {
                    "id": "vid-1",
                    "mediaUrl": "https://imagine-public.x.ai/imagine-public/share-videos/vid-1.mp4",
                    "thumbnailImageUrl": "https://imagine-public.x.ai/.../vid-1_thumbnail.jpg",
                    "videos": [
                        {
                            "id": "vid-1",
                            "mediaUrl": "https://imagine-public.x.ai/.../vid-1.mp4",
                            "thumbnailImageUrl": "https://imagine-public.x.ai/.../vid-1_thumbnail.jpg",
                        }
                    ],
                }
            }
        )
        assert await c.check_video_moderated("vid-1") is False

    @pytest.mark.asyncio
    async def test_empty_mediaurl_is_moderated(self):
        c = _stub_client()
        c._api_request = AsyncMock(
            return_value={
                "post": {
                    "id": "vid-m",
                    "mediaUrl": "",
                    "thumbnailImageUrl": f"https://imagine-public.x.ai/imagine-public/images/{MODERATED_THUMBNAIL_UUID}.png",
                    "videos": [
                        {
                            "id": "vid-m",
                            "mediaUrl": "",
                            "thumbnailImageUrl": f"https://.../{MODERATED_THUMBNAIL_UUID}.png",
                        }
                    ],
                }
            }
        )
        assert await c.check_video_moderated("vid-m") is True

    @pytest.mark.asyncio
    async def test_placeholder_thumbnail_with_populated_mediaurl_still_moderated(self):
        """Edge case: if thumbnail is the fixed moderated placeholder, treat as moderated
        regardless of mediaUrl."""
        c = _stub_client()
        c._api_request = AsyncMock(
            return_value={
                "post": {
                    "id": "vid-m",
                    "mediaUrl": "https://legit.url/x.mp4",  # populated
                    "thumbnailImageUrl": f"https://.../{MODERATED_THUMBNAIL_UUID}.png",
                    "videos": [],
                }
            }
        )
        assert await c.check_video_moderated("vid-m") is True

    @pytest.mark.asyncio
    async def test_post_ok_but_child_video_moderated(self):
        """Root post ok but child video moderated → moderated."""
        c = _stub_client()
        c._api_request = AsyncMock(
            return_value={
                "post": {
                    "id": "vid",
                    "mediaUrl": "https://ok.url/v.mp4",
                    "thumbnailImageUrl": "https://ok.url/v_thumbnail.jpg",
                    "videos": [{"id": "vid", "mediaUrl": "", "thumbnailImageUrl": ""}],
                }
            }
        )
        assert await c.check_video_moderated("vid") is True

    @pytest.mark.asyncio
    async def test_no_videos_list_falls_through_to_post_check(self):
        c = _stub_client()
        c._api_request = AsyncMock(
            return_value={
                "post": {
                    "id": "vid",
                    "mediaUrl": "https://ok.url/v.mp4",
                    "thumbnailImageUrl": "https://ok.url/v_thumbnail.jpg",
                }  # no videos key
            }
        )
        assert await c.check_video_moderated("vid") is False


class TestCreateVideoVerifyFinal:
    @pytest.mark.asyncio
    async def test_verify_final_flips_moderated_when_post_is_placeholder(self):
        """create_video(verify_final=True) overrides moderated=False when post page says otherwise."""
        c = _stub_client()
        initial = VideoGenerationResult(video_id="v1", parent_post_id="p1", moderated=False)
        with patch.object(GrokClient, "_create_video_from_upload", AsyncMock(return_value=initial)):
            with patch.object(
                GrokClient, "check_video_moderated", AsyncMock(return_value=True)
            ) as cmv:
                r = await c.create_video(
                    {
                        "images": ["./a.jpg"],
                        "prompt": "x",
                        "verify_final": True,
                    }
                )
        cmv.assert_awaited_once_with("v1")
        assert r.moderated is True

    @pytest.mark.asyncio
    async def test_verify_final_noop_when_already_moderated(self):
        """If immediate result already says moderated, skip the REST check."""
        c = _stub_client()
        initial = VideoGenerationResult(video_id="v1", parent_post_id="p1", moderated=True)
        with patch.object(GrokClient, "_create_video_from_upload", AsyncMock(return_value=initial)):
            with patch.object(
                GrokClient, "check_video_moderated", AsyncMock(return_value=False)
            ) as cmv:
                r = await c.create_video(
                    {
                        "images": ["./a.jpg"],
                        "prompt": "x",
                        "verify_final": True,
                    }
                )
        cmv.assert_not_awaited()
        assert r.moderated is True  # unchanged

    @pytest.mark.asyncio
    async def test_verify_final_off_by_default(self):
        """Without verify_final, no extra REST call is made."""
        c = _stub_client()
        initial = VideoGenerationResult(video_id="v1", parent_post_id="p1", moderated=False)
        with patch.object(GrokClient, "_create_video_from_upload", AsyncMock(return_value=initial)):
            with patch.object(
                GrokClient, "check_video_moderated", AsyncMock(return_value=True)
            ) as cmv:
                r = await c.create_video({"images": ["./a.jpg"], "prompt": "x"})
        cmv.assert_not_awaited()
        assert r.moderated is False

    @pytest.mark.asyncio
    async def test_verify_final_tolerates_rest_error(self):
        """If the REST check itself fails, we keep the original moderated value
        rather than bubbling the error — caller already has a video_id."""
        c = _stub_client()
        initial = VideoGenerationResult(video_id="v1", parent_post_id="p1", moderated=False)
        with patch.object(GrokClient, "_create_video_from_upload", AsyncMock(return_value=initial)):
            with patch.object(
                GrokClient, "check_video_moderated", AsyncMock(side_effect=RuntimeError("network"))
            ):
                r = await c.create_video(
                    {
                        "images": ["./a.jpg"],
                        "prompt": "x",
                        "verify_final": True,
                    }
                )
        assert r.moderated is False  # unchanged on error
