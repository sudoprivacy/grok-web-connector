"""Unit tests for the 2026 canvas/conversation generation enumeration.

Root cause (consumer CDP capture + our recon, 2026-08): list_posts reads the
legacy /rest/media/post/list surface = LIKED posts only, so a user's OWN 2026
Imagine generations are invisible. They live under app-chat conversations:
  GET /rest/app-chat/conversations                -> {conversations:[{conversationId,...}]}
  GET /rest/app-chat/conversations/{id}/responses -> {responses:[{fileAttachmentsMetadata,generatedImageUrls,...}]}
list_generations() / find_generation_by_id() walk that surface. These tests
pin the pure extractor + the method routing; the live e2e run is
tests/integration/test_workflows.py::test_list_generations_enumerates_own_media.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from grok_web import GenerationMedia, GrokClient
from grok_web._internal import (
    _asset_url,
    _mime_to_type,
    classify_media_url,
    extract_generation_media,
    normalize_asset_id,
)

# A captured-shape /responses payload (redacted): the real fields are
# fileAttachmentsMetadata (fileMetadataId/fileMimeType/fileUri) + the relative
# generatedImageUrls paths. 0531 appears in both fields -> must dedup.
REAL_RESPONSES = {
    "responses": [
        {
            "responseId": "resp-1",
            "createTime": "2026-01-10T08:52:53Z",
            "generatedImageUrls": [
                "users/uid/generated/0531ae5e-6aa0-44a6-a7b5-6f2d525911c3/image.jpg",
                "users/uid/generated/faddb806-269d-4a86-a658-bb48667c9f92/image.jpg",
            ],
            "fileUris": ["0531ae5e-6aa0-44a6-a7b5-6f2d525911c3"],
            "fileAttachmentsMetadata": [
                {
                    "fileMetadataId": "0531ae5e-6aa0-44a6-a7b5-6f2d525911c3",
                    "fileMimeType": "image/jpeg",
                    "fileName": "image.jpg",
                    "fileUri": "users/uid/generated/0531ae5e-6aa0-44a6-a7b5-6f2d525911c3/image.jpg",
                }
            ],
            # web-search citations in the same response must NOT be mistaken for media
            "webSearchResults": [{"url": "https://cnn.com/news/x.jpg"}],
        }
    ]
}

# The reporter's case: a VIDEO (video/mp4) whose fileMetadataId is a UUIDv7 —
# the id that names their grok-video-<uuid>.mp4 download.
VIDEO_RESPONSES = {
    "responses": [
        {
            "responseId": "resp-v",
            "createTime": "2026-08-01T00:00:00Z",
            "fileAttachmentsMetadata": [
                {
                    "fileMetadataId": "019f7a1e-a1c2-71a2-917b-866854bca7e2",
                    "fileMimeType": "video/mp4",
                    "fileName": "video.mp4",
                    "fileUri": "users/uid/generated/019f7a1e-a1c2-71a2-917b-866854bca7e2/video.mp4",
                }
            ],
        }
    ]
}


def _run(coro):
    return asyncio.run(coro)


def _client():
    return GrokClient.__new__(GrokClient)


class TestClassifyAndNormalize:
    def test_relative_image_path(self):
        assert classify_media_url("users/u/generated/abc-123/image.jpg") == ("image", "abc-123")

    def test_relative_video_path_uses_generated_id(self):
        assert classify_media_url("users/u/generated/vid-9/video.mp4") == ("video", "vid-9")

    def test_full_grok_url(self):
        assert classify_media_url("https://assets.grok.com/x/y/z.mp4")[0] == "video"

    def test_non_grok_host_excluded(self):
        assert classify_media_url("https://cnn.com/news/x.jpg") == (None, None)

    def test_no_extension_excluded(self):
        assert classify_media_url("https://grok.com/imagine/post/abc") == (None, None)

    def test_mime_to_type(self):
        assert _mime_to_type("image/jpeg") == "image"
        assert _mime_to_type("video/mp4") == "video"
        assert _mime_to_type("audio/mpeg") == "audio"
        assert _mime_to_type("application/json") is None
        assert _mime_to_type(None) is None

    def test_asset_url_templating(self):
        assert _asset_url("users/u/generated/x/image.jpg") == (
            "https://assets.grok.com/users/u/generated/x/image.jpg"
        )
        assert _asset_url("https://assets.grok.com/a.mp4") == "https://assets.grok.com/a.mp4"
        assert _asset_url(None) is None

    def test_normalize_asset_id(self):
        u = "019f7a1e-a1c2-71a2-917b-866854bca7e2"
        assert normalize_asset_id(f"grok-video-{u}.mp4") == u
        assert normalize_asset_id(f"grok-image-{u}.jpg") == u
        assert normalize_asset_id(u) == u
        assert normalize_asset_id(f"https://assets.grok.com/users/x/generated/{u}/video.mp4") == u
        assert normalize_asset_id("some-file.png") == "some-file"  # no uuid -> stem


class TestExtractGenerationMedia:
    def test_real_structure_dedups_by_asset_id(self):
        items = extract_generation_media(REAL_RESPONSES, "conv-1")
        assert len(items) == 2  # 0531 in two fields -> one item
        ids = {it["asset_id"] for it in items}
        assert ids == {
            "0531ae5e-6aa0-44a6-a7b5-6f2d525911c3",
            "faddb806-269d-4a86-a658-bb48667c9f92",
        }
        assert all(it["media_type"] == "image" for it in items)
        assert all(it["url"].startswith("https://assets.grok.com/users/") for it in items)
        assert all(it["conversation_id"] == "conv-1" for it in items)

    def test_web_search_urls_not_captured(self):
        items = extract_generation_media(REAL_RESPONSES, "c")
        assert not any("cnn.com" in it["url"] for it in items)

    def test_video_mime_and_id(self):
        items = extract_generation_media(VIDEO_RESPONSES, "conv-v")
        assert len(items) == 1
        assert items[0]["media_type"] == "video"
        assert items[0]["asset_id"] == "019f7a1e-a1c2-71a2-917b-866854bca7e2"

    def test_empty_and_malformed(self):
        assert extract_generation_media({}, "c") == []
        assert extract_generation_media({"responses": [None, 3, "x"]}, "c") == []

    def test_model_coerces_createtime(self):
        gm = GenerationMedia(**extract_generation_media(VIDEO_RESPONSES, "c")[0])
        assert gm.media_type == "video"
        assert gm.created_at is not None


class TestListGenerationsRouting:
    def _api_side_effect(self):
        async def fake(method, endpoint, json_data=None):
            if endpoint.endswith("/responses"):
                if "conv-img" in endpoint:
                    return REAL_RESPONSES
                if "conv-vid" in endpoint:
                    return VIDEO_RESPONSES
                return {"responses": []}
            # conversation list
            return {
                "conversations": [
                    {"conversationId": "conv-img"},
                    {"conversationId": "conv-vid"},
                    {"conversationId": "conv-empty"},
                ]
            }

        return fake

    def test_enumerates_across_conversations(self):
        c = _client()
        c._api_request = AsyncMock(side_effect=self._api_side_effect())
        gens = _run(c.list_generations())
        # 2 images (conv-img) + 1 video (conv-vid), newest-first (video is 2026-08)
        assert len(gens) == 3
        assert gens[0].media_type == "video"  # 2026-08 sorts before 2026-01
        assert {g.media_type for g in gens} == {"image", "video"}

    def test_media_type_filter(self):
        c = _client()
        c._api_request = AsyncMock(side_effect=self._api_side_effect())
        vids = _run(c.list_generations(media_type="video"))
        assert len(vids) == 1 and vids[0].media_type == "video"

    def test_limit_applied(self):
        c = _client()
        c._api_request = AsyncMock(side_effect=self._api_side_effect())
        assert len(_run(c.list_generations(limit=1))) == 1

    def test_find_by_id_via_enumeration_fallback(self):
        # conv-vid has a single video whose asset_id equals this id -> enumeration
        # fallback matches it (the id happens to be an asset id, not a convId).
        c = _client()
        c._api_request = AsyncMock(side_effect=self._api_side_effect())
        hit = _run(c.find_generation_by_id("grok-image-019f7a1e-a1c2-71a2-917b-866854bca7e2.mp4"))
        assert hit is not None and hit.media_type == "video"

    def test_find_by_id_missing_returns_none(self):
        c = _client()
        c._api_request = AsyncMock(side_effect=self._api_side_effect())
        assert _run(c.find_generation_by_id("does-not-exist")) is None

    def test_per_conversation_failure_is_skipped(self):
        from grok_web.exceptions import GrokAPIError

        c = _client()

        async def flaky(method, endpoint, json_data=None):
            if endpoint.endswith("/responses"):
                if "conv-img" in endpoint:
                    return REAL_RESPONSES
                raise GrokAPIError("boom")  # conv-vid blows up
            return {
                "conversations": [{"conversationId": "conv-img"}, {"conversationId": "conv-vid"}]
            }

        c._api_request = AsyncMock(side_effect=flaky)
        gens = _run(c.list_generations())
        assert len(gens) == 2  # still get conv-img's images despite conv-vid failing


# A conversation fetched DIRECTLY by id (the download-filename id is a
# conversationId) holding THREE videos with distinct asset ids/sizes — the real
# shape: one filename id -> many generations, disambiguated by local file size.
CONV_ID = "019f7a1e-a1c2-71a2-917b-866854bca7e2"
MULTI_VIDEO = {
    "responses": [
        {
            "responseId": f"r{i}",
            "createTime": f"2026-08-0{i}T00:00:00Z",
            "fileAttachmentsMetadata": [
                {
                    "fileMetadataId": aid,
                    "fileMimeType": "video/mp4",
                    "fileName": "generated_video.mp4",
                    "fileUri": f"users/u/generated/{aid}/generated_video.mp4",
                }
            ],
        }
        for i, aid in enumerate(
            [
                "aaaa1111-0000-4000-8000-000000000001",
                "bbbb2222-0000-4000-8000-000000000002",
                "cccc3333-0000-4000-8000-000000000003",
            ],
            start=1,
        )
    ]
}
SIZE_BY_ASSET = {
    "aaaa1111-0000-4000-8000-000000000001": 1000,
    "bbbb2222-0000-4000-8000-000000000002": 2000,
    "cccc3333-0000-4000-8000-000000000003": 3000,
}


class TestConversationResolver:
    """The download-filename id is a conversationId — resolve it DIRECTLY by id
    (works even when it's not in the windowed conversation list) + size-match."""

    def _client_with_conv(self):
        c = _client()

        async def fake(method, endpoint, json_data=None):
            if CONV_ID in endpoint and endpoint.endswith("/responses"):
                return MULTI_VIDEO
            if endpoint.endswith("/responses"):
                return {"responses": []}
            return {"conversations": []}  # empty windowed list — id not in it

        c._api_request = AsyncMock(side_effect=fake)

        async def size_of(url):
            for aid, sz in SIZE_BY_ASSET.items():
                if aid in url:
                    return sz
            return -1

        c.get_asset_file_size = AsyncMock(side_effect=size_of)
        return c

    def test_get_conversation_media_direct_by_id(self):
        c = self._client_with_conv()
        media = _run(c.get_conversation_media(f"grok-video-{CONV_ID}.mp4", media_type="video"))
        assert len(media) == 3
        assert all(m.media_type == "video" for m in media)
        assert all(m.conversation_id == CONV_ID for m in media)

    def test_find_by_id_size_picks_exact_video(self):
        c = self._client_with_conv()
        hit = _run(c.find_generation_by_id(f"grok-video-{CONV_ID}.mp4", size=2000))
        assert hit is not None
        assert hit.asset_id == "bbbb2222-0000-4000-8000-000000000002"

    def test_find_by_id_multi_video_no_size_is_ambiguous(self):
        c = self._client_with_conv()
        # 3 videos, no size -> cannot disambiguate -> None (use get_conversation_media)
        assert _run(c.find_generation_by_id(CONV_ID)) is None

    def test_find_by_id_single_video_returns_it(self):
        c = _client()

        async def fake(method, endpoint, json_data=None):
            if endpoint.endswith("/responses"):
                return VIDEO_RESPONSES  # single video
            return {"conversations": []}

        c._api_request = AsyncMock(side_effect=fake)
        hit = _run(c.find_generation_by_id("some-conv-id"))
        assert hit is not None and hit.media_type == "video"

    def test_get_conversation_media_not_found_raises(self):
        from grok_web.exceptions import GrokNotFoundError

        c = _client()

        async def fake(method, endpoint, json_data=None):
            raise GrokNotFoundError("Resource not found")

        c._api_request = AsyncMock(side_effect=fake)
        try:
            _run(c.get_conversation_media("missing"))
            raise AssertionError("expected GrokNotFoundError")
        except GrokNotFoundError:
            pass
