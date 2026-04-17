"""Unit tests for the direct-REST path (file: references)."""

import json

import pytest

from grok_web.actions.direct_rest import (
    StatsigSnitch,
    build_video_submit_payload,
)
from grok_web.prompt_parser import classify_image_source


class TestClassifyImageSource:
    """classify_image_source must distinguish post:, file:, and raw paths."""

    def test_post_prefix(self):
        assert classify_image_source("post:abc-123") == ("post", "abc-123")

    def test_file_prefix(self):
        assert classify_image_source("file:477c03f8-f4ca") == ("upload", "477c03f8-f4ca")

    def test_raw_path(self):
        assert classify_image_source("./frame.jpg") == ("file", "./frame.jpg")

    def test_absolute_path(self):
        assert classify_image_source("C:/tmp/a.png") == ("file", "C:/tmp/a.png")


class TestBuildVideoSubmitPayload:
    """Payload shape must match what Grok's frontend sends."""

    def test_single_file_shape(self):
        p = build_video_submit_payload(
            file_ids=["f1"],
            file_uris=["users/u1/f1/content"],
            parent_post_id="post-1",
            prompt="zoom slowly",
            duration=6,
            resolution="480p",
            aspect_ratio="2:3",
        )
        assert p["fileAttachments"] == ["f1"]  # single: list with the file id
        vc = p["responseMetadata"]["modelConfigOverride"]["modelMap"]["videoGenModelConfig"]
        assert vc["parentPostId"] == "post-1"
        # Single-file payload does not include isReferenceToVideo
        assert "isReferenceToVideo" not in vc
        # Message contains the asset URL + prompt + --mode=custom
        assert "https://assets.grok.com/users/u1/f1/content" in p["message"]
        assert "zoom slowly" in p["message"]
        assert "--mode=custom" in p["message"]

    def test_single_file_empty_prompt_uses_mode_normal(self):
        p = build_video_submit_payload(
            file_ids=["f1"],
            file_uris=["users/u1/f1/content"],
            parent_post_id="post-1",
            prompt="",
            duration=6,
            resolution="480p",
            aspect_ratio=None,
        )
        assert "--mode=normal" in p["message"]
        assert "--mode=custom" not in p["message"]

    def test_multi_file_shape(self):
        p = build_video_submit_payload(
            file_ids=["f1", "f2"],
            file_uris=["users/u1/f1/content", "users/u1/f2/content"],
            parent_post_id="post-xyz",
            prompt="zoom @1 @2",
            duration=10,
            resolution="720p",
            aspect_ratio="16:9",
        )
        # Multi-file: fileAttachments is null, not a list
        assert p["fileAttachments"] is None
        vc = p["responseMetadata"]["modelConfigOverride"]["modelMap"]["videoGenModelConfig"]
        assert vc["parentPostId"] == "post-xyz"
        assert vc["isReferenceToVideo"] is True
        assert vc["imageReferences"] == [
            "https://assets.grok.com/users/u1/f1/content",
            "https://assets.grok.com/users/u1/f2/content",
        ]
        # @N refs in the prompt serialize to @<file_id>
        assert "@f1" in p["message"]
        assert "@f2" in p["message"]
        assert "--mode=custom" in p["message"]

    def test_multi_file_without_refs_in_prompt(self):
        p = build_video_submit_payload(
            file_ids=["f1", "f2"],
            file_uris=["users/u1/f1/content", "users/u1/f2/content"],
            parent_post_id="post-xyz",
            prompt="slow zoom",
            duration=6,
            resolution="480p",
            aspect_ratio=None,
        )
        # Prompt is preserved literally (no @N to rewrite)
        assert "slow zoom" in p["message"]
        # Still adds --mode=custom because prompt is non-empty
        assert "--mode=custom" in p["message"]

    def test_multi_file_serializable(self):
        """The payload must survive JSON round-trip (tab.evaluate requires this)."""
        p = build_video_submit_payload(
            file_ids=["f1", "f2"],
            file_uris=["users/u1/f1/content", "users/u1/f2/content"],
            parent_post_id="pp",
            prompt="@1 @2",
            duration=6,
            resolution="480p",
            aspect_ratio="2:3",
        )
        restored = json.loads(json.dumps(p))
        assert restored["fileAttachments"] is None


class TestStatsigSnitch:
    """StatsigSnitch caches per-endpoint sids and returns the right one."""

    @pytest.fixture
    def snitch(self):
        # Bypass install() — we only test cache logic here.
        return StatsigSnitch(tab=None)

    def test_cache_starts_empty(self, snitch):
        assert snitch.latest is None
        assert snitch._by_endpoint == {}

    def test_latest_prefers_conversations_new(self, snitch):
        snitch._by_endpoint["/rest/media/post/create"] = "create-sid"
        snitch._by_endpoint["/rest/app-chat/conversations/new"] = "conv-sid"
        assert snitch.latest == "conv-sid"

    def test_latest_falls_back_when_conv_missing(self, snitch):
        snitch._by_endpoint["/rest/media/post/create"] = "create-sid"
        assert snitch.latest == "create-sid"

    @pytest.mark.asyncio
    async def test_get_returns_endpoint_specific_sid(self, snitch):
        snitch._by_endpoint["/rest/app-chat/conversations/new"] = "conv-sid"
        snitch._by_endpoint["/rest/media/post/create"] = "create-sid"
        assert await snitch.get("/rest/app-chat/conversations/new", timeout=0.1) == "conv-sid"
        assert await snitch.get("/rest/media/post/create", timeout=0.1) == "create-sid"

    @pytest.mark.asyncio
    async def test_get_returns_none_on_timeout(self, snitch):
        assert await snitch.get("/rest/app-chat/conversations/new", timeout=0.1) is None
